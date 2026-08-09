from __future__ import annotations

import json
import re
import threading

import uvicorn
from pydantic import BaseModel, Field

import speed_patch as runtime

core = runtime.core

EMOJI_FILE = core.BASE_DIR / ".emoji_rules.json"
RUNTIME_WEB_FILE = core.BASE_DIR / ".runtime_emoji_index.html"
MAX_RULES = 200

_rules_lock = threading.Lock()
_emoji_rules: dict[str, str] = {}
_previous_normalize = core._normalize_abbreviations


class EmojiRulesRequest(BaseModel):
    rules: dict[str, str] = Field(default_factory=dict)


def _sanitize_rules(value: dict[str, str]) -> dict[str, str]:
    clean: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip()
        target = re.sub(r"\s+", " ", str(raw_value or "")).strip()
        if not key or len(key) > 64 or len(target) > 120:
            continue
        clean[key] = target
        if len(clean) >= MAX_RULES:
            break
    return clean


def _load_rules() -> None:
    global _emoji_rules
    try:
        if EMOJI_FILE.exists():
            data = json.loads(EMOJI_FILE.read_text(encoding="utf-8"))
            raw = data.get("rules", {}) if isinstance(data, dict) else {}
            if isinstance(raw, dict):
                _emoji_rules = _sanitize_rules(raw)
    except Exception as exc:
        print(f"[EMOJI] Không đọc được bộ emoji, dùng rỗng: {exc}")
        _emoji_rules = {}


def _save_rules() -> None:
    EMOJI_FILE.write_text(
        json.dumps({"rules": _emoji_rules}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _get_rules() -> dict[str, str]:
    with _rules_lock:
        return dict(_emoji_rules)


def _apply_rules(text: str, rules: dict[str, str]) -> tuple[str, list[tuple[str, str, int]]]:
    output = str(text or "")
    changed: list[tuple[str, str, int]] = []

    # Longest rule first so compound emoji such as ❤️‍🔥 wins over shorter parts.
    for source in sorted(rules, key=len, reverse=True):
        target = rules[source]
        if not source:
            continue

        # Collapse one or more consecutive identical emoji into one spoken phrase.
        pattern = re.compile(rf"(?:{re.escape(source)})+")
        replacement = f" {target} " if target else " "
        output, count = pattern.subn(replacement, output)
        if count:
            changed.append((source, target, count))

    if changed:
        output = re.sub(r"\s+", " ", output)
        output = re.sub(r"\s+([,.;:!?])", r"\1", output)
        output = re.sub(r"^[\s,.;:!?…|/\\\-–—]+", "", output)
        output = re.sub(r"[\s,.;:!?…|/\\\-–—]+$", "", output)
        output = output.strip()

    return output, changed


def _apply_configured_emoji(text: str) -> tuple[str, list[tuple[str, str, int]]]:
    return _apply_rules(text, _get_rules())


def _patched_normalize_abbreviations(text: str, cfg: core.SpeakerSettings) -> tuple[str, int]:
    emoji_text, changed = _apply_configured_emoji(text)
    if changed:
        core.stats["comments_emoji_normalized"] = core.stats.get("comments_emoji_normalized", 0) + 1
        print(f"[EMOJI] {changed} | {text} -> {emoji_text or '[RỖNG]'}")
    return _previous_normalize(emoji_text, cfg)


@core.app.get("/api/emoji-rules")
def get_emoji_rules():
    return {"ok": True, "rules": _get_rules(), "maxRules": MAX_RULES}


@core.app.post("/api/emoji-rules")
def set_emoji_rules(req: EmojiRulesRequest):
    global _emoji_rules
    clean = _sanitize_rules(req.rules)
    with _rules_lock:
        _emoji_rules = clean
        _save_rules()
    print(f"[EMOJI] Đã lưu {len(clean)} quy tắc emoji")
    return {"ok": True, "rules": clean, "count": len(clean)}


def _patch_web() -> None:
    html = core.WEB_FILE.read_text(encoding="utf-8")

    emoji_html = r'''
    <div style="margin-top:12px">
      <label>Emoji → cách đọc: mỗi dòng <b>emoji = cách đọc</b></label>
      <textarea id="emojiRules" placeholder="🤣=cười lớn&#10;😭=khóc rồi&#10;❤️=thả tim&#10;🔥="></textarea>
      <div class="hint">Bạn tự đặt hoàn toàn. Để trống bên phải dấu = để xóa emoji không đọc. Emoji giống nhau lặp liên tiếp chỉ đọc một lần. Emoji chưa khai báo sẽ giữ nguyên.</div>
      <button id="saveEmojiRules" style="margin-top:10px">LƯU BỘ EMOJI</button>
    </div>

'''
    marker = '<details style="margin-top:12px">'
    if 'id="emojiRules"' not in html:
        html = html.replace(marker, emoji_html + marker)

    emoji_js = r'''
function emojiRulesToText(obj){
  return Object.entries(obj||{}).map(([k,v])=>`${k}=${v}`).join('\n');
}
function parseEmojiRules(text){
  const out={};
  for(const raw of String(text||'').split(/\r?\n/)){
    const line=raw.trim();
    if(!line||line.startsWith('#'))continue;
    const pos=line.indexOf('=');
    if(pos<=0)continue;
    const key=line.slice(0,pos).trim();
    const value=line.slice(pos+1).trim();
    if(key)out[key]=value;
  }
  return out;
}
async function loadEmojiRules(){
  const r=await fetch('/api/emoji-rules',{cache:'no-store'}),d=await r.json();
  if(!r.ok)throw new Error(JSON.stringify(d));
  $('emojiRules').value=emojiRulesToText(d.rules||{});
  return Object.keys(d.rules||{}).length;
}
async function saveEmojiRules(){
  const rules=parseEmojiRules($('emojiRules').value);
  const r=await fetch('/api/emoji-rules',{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rules})
  });
  const d=await r.json();
  if(!r.ok)throw new Error(JSON.stringify(d));
  return Number(d.count||0);
}

'''
    if "async function loadEmojiRules()" not in html:
        html = html.replace("async function loadVoices(){", emoji_js + "async function loadVoices(){")

    html = html.replace(
        "await loadVoices();await loadSettings();await loadPlaybackSpeed();",
        "await loadVoices();await loadSettings();await loadPlaybackSpeed();await loadEmojiRules();",
    )

    emoji_listener = r'''
$('saveEmojiRules').addEventListener('click',async()=>{
  try{
    const count=await saveEmojiRules();
    $('settingsState').textContent=`✅ Đã lưu ${count} quy tắc emoji.`;
  }catch(e){
    $('settingsState').textContent='Lỗi lưu bộ emoji: '+e;
  }
});
'''
    if "$('saveEmojiRules').addEventListener" not in html:
        html = html.replace("async function boot(){", emoji_listener + "\nasync function boot(){")

    RUNTIME_WEB_FILE.write_text(html, encoding="utf-8")
    core.WEB_FILE = RUNTIME_WEB_FILE

    required = (
        'id="emojiRules"',
        'id="saveEmojiRules"',
        "loadEmojiRules",
        "/api/emoji-rules",
    )
    missing = [item for item in required if item not in html]
    if missing:
        raise RuntimeError(f"Không chèn được UI emoji: thiếu {missing}")


_load_rules()
core._normalize_abbreviations = _patched_normalize_abbreviations
core.stats.setdefault("comments_emoji_normalized", 0)
_patch_web()
core.SERVER_VERSION = "2.6"
core.app.version = "2.6"


# Startup guards for exact emoji behavior and repeated-emoji collapsing.
assert _apply_rules("🤣🤣🤣", {"🤣": "cười lớn"})[0] == "cười lớn"
assert _apply_rules("hay quá 🤣🤣🤣", {"🤣": "cười lớn"})[0] == "hay quá cười lớn"
assert _apply_rules("😭😭", {"😭": ""})[0] == ""
assert _apply_rules("chị ơi ❤️❤️", {"❤️": "thả tim"})[0] == "chị ơi thả tim"
assert _apply_rules("🤣😭", {"🤣": "cười", "😭": "khóc"})[0] == "cười khóc"


if __name__ == "__main__":
    print(f"[EMOJI] Custom rules: {len(_get_rules())} | lặp cùng emoji chỉ đọc 1 lần")
    print("[EMOJI] Dùng emoji= để xóa không đọc; emoji chưa khai báo giữ nguyên.")
    uvicorn.run(core.app, host=core.HOST, port=core.PORT, log_level="info")
