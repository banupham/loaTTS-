from __future__ import annotations

import json
import re
import threading

import uvicorn
from pydantic import BaseModel, Field

import pipeline_runtime as runtime

core = runtime.core

PROSODY_FILE = core.BASE_DIR / ".prosody_settings.json"
RUNTIME_WEB_FILE = core.BASE_DIR / ".runtime_prosody_index.html"

_prosody_lock = threading.Lock()
_prosody_mode = "clear"


class ProsodyRequest(BaseModel):
    mode: str = Field(default="clear")


def _sanitize_mode(value: str) -> str:
    mode = str(value or "clear").strip().lower()
    return mode if mode in {"normal", "clear", "slow"} else "clear"


def _load_prosody() -> None:
    global _prosody_mode
    try:
        if PROSODY_FILE.exists():
            data = json.loads(PROSODY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _prosody_mode = _sanitize_mode(data.get("mode"))
    except Exception as exc:
        print(f"[PROSODY] Không đọc được cấu hình, dùng clear: {exc}")
        _prosody_mode = "clear"


def _save_prosody() -> None:
    PROSODY_FILE.write_text(
        json.dumps({"mode": _prosody_mode}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _get_mode() -> str:
    with _prosody_lock:
        return _prosody_mode


def _apply_reading_rhythm(text: str, mode: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    mode = _sanitize_mode(mode)
    if not value or mode == "normal":
        return value

    # Không kéo chậm PCM. Chỉ tạo nhịp ngay trên text trước khi VieNeu sinh audio.
    # clear: khoảng 6 từ/nhịp; slow: khoảng 3 từ/nhịp.
    words_per_pause = 6 if mode == "clear" else 3
    tokens = value.split(" ")
    output: list[str] = []
    since_pause = 0

    for index, token in enumerate(tokens):
        output.append(token)
        since_pause += 1

        # Dấu câu có sẵn đã tạo một nhịp tự nhiên, không chèn thêm.
        if re.search(r"[,;:.!?…][\"'”’)]?$", token):
            since_pause = 0
            continue

        if since_pause >= words_per_pause and index < len(tokens) - 1:
            output[-1] = token + ","
            since_pause = 0

    result = " ".join(output).strip()
    if result and not re.search(r"[.!?…][\"'”’)]?$", result):
        result += "."
    return result


@core.app.get("/api/prosody")
def get_prosody():
    mode = _get_mode()
    return {
        "ok": True,
        "mode": mode,
        "modes": {
            "normal": "Bình thường - giữ nguyên text",
            "clear": "Rõ chữ - khoảng 6 từ mỗi nhịp",
            "slow": "Chậm tách ý - khoảng 3 từ mỗi nhịp",
        },
    }


@core.app.post("/api/prosody")
def set_prosody(req: ProsodyRequest):
    global _prosody_mode
    mode = _sanitize_mode(req.mode)
    with _prosody_lock:
        _prosody_mode = mode
        _save_prosody()
    print(f"[PROSODY] Nhịp đọc -> {mode}")
    return {"ok": True, "mode": mode}


def _render_job_sync_with_prosody(job) -> bytes:
    if core.tts is None:
        raise RuntimeError("Model chưa sẵn sàng")

    raw_text = core._comment_to_tts_text(job)
    mode = _get_mode()
    text = _apply_reading_rhythm(raw_text, mode)
    if text != raw_text:
        print(f"[PROSODY] {mode}: {raw_text} -> {text}")

    with core.settings_lock:
        cfg = core.settings.model_copy(deep=True)
    core._validate_voice(cfg.voice)

    pcm_all = bytearray()
    started = runtime.time.perf_counter()
    chunk_count = 0

    with core.tts_lock:
        for audio in core.tts.infer_stream(
            text,
            voice=cfg.voice,
            style=cfg.style,
            temperature=cfg.temperature,
            top_k=cfg.top_k,
            top_p=cfg.top_p,
            max_chars=cfg.max_chars,
            repetition_penalty=cfg.repetition_penalty,
            apply_watermark=False,
        ):
            pcm = core._pcm16_bytes(audio)
            if pcm:
                pcm_all.extend(pcm)
                chunk_count += 1

    elapsed = runtime.time.perf_counter() - started
    print(
        f"[GEN] Xong {job.id} | {elapsed:.2f}s | chunks={chunk_count} "
        f"| pcm={len(pcm_all) / 1024:.1f} KiB | rhythm={mode} "
        f"| {job.display_name}: {job.text[:80]}"
    )
    return bytes(pcm_all)


def _patch_web() -> None:
    html = core.WEB_FILE.read_text(encoding="utf-8")

    prosody_html = r'''
    <div style="margin-top:10px">
      <label>Nhịp đọc · tạo nhịp ngay khi sinh giọng, không đổi tốc độ phát audio</label>
      <select id="readingRhythm">
        <option value="normal">Bình thường — giữ nguyên câu</option>
        <option value="clear">Rõ chữ — khoảng 6 từ / nhịp</option>
        <option value="slow">Chậm tách ý — khoảng 3 từ / nhịp</option>
      </select>
      <div class="hint">Rõ chữ/Chậm sẽ chèn dấu ngắt nhẹ vào text trước VieNeu. PCM vẫn phát 1.00x.</div>
    </div>
'''
    if 'id="readingRhythm"' not in html:
        marker = '    <div class="advanced">'
        if marker not in html:
            raise RuntimeError("Không tìm thấy vị trí chèn Nhịp đọc.")
        html = html.replace(marker, prosody_html + "\n" + marker, 1)

    prosody_js = r'''
async function loadProsody(){
  const r=await fetch('/api/prosody',{cache:'no-store'}),d=await r.json();
  if(!r.ok)throw new Error(JSON.stringify(d));
  $('readingRhythm').value=d.mode||'clear';
}
async function saveProsody(){
  const mode=$('readingRhythm').value||'clear';
  const r=await fetch('/api/prosody',{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})
  });
  const d=await r.json();
  if(!r.ok)throw new Error(JSON.stringify(d));
  return d.mode||mode;
}

'''
    if "async function loadProsody()" not in html:
        marker_js = "async function loadVoices(){"
        if marker_js not in html:
            raise RuntimeError("Không tìm thấy loadVoices() để chèn JS prosody.")
        html = html.replace(marker_js, prosody_js + marker_js, 1)

    if "await loadProsody();" not in html:
        marker_boot = "$('settingsState').textContent='Cấu hình giọng và bộ lọc đã tải.';"
        if marker_boot not in html:
            raise RuntimeError("Không tìm thấy boot marker để nạp Nhịp đọc.")
        html = html.replace(
            marker_boot,
            "await loadProsody();\n    " + marker_boot,
            1,
        )

    old_handler = "try{await saveSettings()}catch(e){$('settingsState').textContent='Lỗi lưu cấu hình: '+e}"
    new_handler = "try{await saveSettings();await saveProsody()}catch(e){$('settingsState').textContent='Lỗi lưu cấu hình: '+e}"
    if "await saveProsody()" not in html:
        if old_handler not in html:
            raise RuntimeError("Không tìm thấy nút lưu cấu hình để gắn Nhịp đọc.")
        html = html.replace(old_handler, new_handler, 1)

    RUNTIME_WEB_FILE.write_text(html, encoding="utf-8")
    core.WEB_FILE = RUNTIME_WEB_FILE

    required = (
        'id="readingRhythm"',
        "loadProsody",
        "saveProsody",
        "/api/prosody",
    )
    missing = [item for item in required if item not in html]
    if missing:
        raise RuntimeError(f"Không chèn được UI Nhịp đọc: thiếu {missing}")


_load_prosody()
runtime._render_job_sync = _render_job_sync_with_prosody
_patch_web()

core.stats.setdefault("prosody_mode_changes", 0)
core.SERVER_VERSION = "3.3"
core.app.version = "3.3"

assert _apply_reading_rhythm("chị ơi còn hàng không", "normal") == "chị ơi còn hàng không"
assert _apply_reading_rhythm("chị ơi còn hàng không", "clear") == "chị ơi còn hàng không."
assert _apply_reading_rhythm("một hai ba bốn năm sáu bảy tám", "clear") == "một hai ba bốn năm sáu, bảy tám."
assert _apply_reading_rhythm("một hai ba bốn năm sáu", "slow") == "một hai ba, bốn năm sáu."


if __name__ == "__main__":
    print("[CORE] v3.3 - PREBUFFER + TEXT PROSODY")
    print(f"[PROSODY] mode={_get_mode()} | playback=1.00x")
    print("[PROSODY] normal=nguyên bản | clear=~6 từ/nhịp | slow=~3 từ/nhịp")
    uvicorn.run(core.app, host=core.HOST, port=core.PORT, log_level="info")
