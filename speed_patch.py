from __future__ import annotations

import json
import threading

import uvicorn
from pydantic import BaseModel, Field

import run as runtime

core = runtime.core

SPEED_FILE = core.BASE_DIR / ".playback_speed.json"
RUNTIME_WEB_FILE = core.BASE_DIR / ".runtime_speed_index.html"
SPEED_MIN = 0.70
SPEED_MAX = 1.50

_speed_lock = threading.Lock()
_speed = 1.0


class PlaybackSpeedRequest(BaseModel):
    speed: float = Field(default=1.0, ge=SPEED_MIN, le=SPEED_MAX)


def _load_speed() -> None:
    global _speed
    try:
        if SPEED_FILE.exists():
            data = json.loads(SPEED_FILE.read_text(encoding="utf-8"))
            value = float(data.get("speed", 1.0))
            _speed = max(SPEED_MIN, min(SPEED_MAX, value))
    except Exception as exc:
        print(f"[SPEED] Không đọc được tốc độ đã lưu, dùng 1.00x: {exc}")
        _speed = 1.0


def _get_speed() -> float:
    with _speed_lock:
        return float(_speed)


def _save_speed(value: float) -> None:
    SPEED_FILE.write_text(
        json.dumps({"speed": value}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


@core.app.get("/api/playback-speed")
def get_playback_speed():
    return {
        "ok": True,
        "speed": _get_speed(),
        "min": SPEED_MIN,
        "max": SPEED_MAX,
    }


@core.app.post("/api/playback-speed")
def set_playback_speed(req: PlaybackSpeedRequest):
    global _speed
    value = round(float(req.speed), 2)
    with _speed_lock:
        _speed = value
        _save_speed(value)
    print(f"[SPEED] Tốc độ đọc = {value:.2f}x")
    return {"ok": True, "speed": value}


def _patch_web() -> None:
    html = core.WEB_FILE.read_text(encoding="utf-8")

    html = html.replace(
        ".advanced{display:grid;grid-template-columns:repeat(5,1fr);",
        ".advanced{display:grid;grid-template-columns:repeat(6,1fr);",
    )

    repetition_html = (
        '<div><label>Repetition</label>'
        '<input id="repetition" type="number" step="0.05" value="1.20"></div>'
    )
    speed_html = (
        '<div><label>Tốc độ <span id="playbackSpeedValue">1.00x</span></label>'
        '<input id="playbackSpeed" type="range" min="0.70" max="1.50" '
        'step="0.05" value="1.00"></div>'
    )
    if 'id="playbackSpeed"' not in html:
        html = html.replace(repetition_html, repetition_html + speed_html)

    speed_helpers = r'''
async function loadPlaybackSpeed(){
  const r=await fetch('/api/playback-speed',{cache:'no-store'}),d=await r.json();
  if(!r.ok)throw new Error(JSON.stringify(d));
  const speed=Math.max(0.70,Math.min(1.50,Number(d.speed||1)));
  $('playbackSpeed').value=speed.toFixed(2);
  $('playbackSpeedValue').textContent=speed.toFixed(2)+'x';
  return speed;
}
async function savePlaybackSpeed(){
  const speed=Math.max(0.70,Math.min(1.50,Number($('playbackSpeed').value||1)));
  $('playbackSpeedValue').textContent=speed.toFixed(2)+'x';
  const r=await fetch('/api/playback-speed',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({speed})
  });
  const d=await r.json();
  if(!r.ok)throw new Error(JSON.stringify(d));
  return Number(d.speed||speed);
}
async function getPlaybackSpeedForComment(){
  try{
    const r=await fetch('/api/playback-speed',{cache:'no-store'}),d=await r.json();
    if(r.ok)return Math.max(0.70,Math.min(1.50,Number(d.speed||1)));
  }catch(e){console.warn('Không lấy được tốc độ mới nhất',e)}
  return Math.max(0.70,Math.min(1.50,Number($('playbackSpeed')?.value||1)));
}

'''
    if "async function getPlaybackSpeedForComment()" not in html:
        html = html.replace("async function loadVoices(){", speed_helpers + "async function loadVoices(){")

    html = html.replace(
        "await ensureAudio();\n    activeAbort=new AbortController();",
        "await ensureAudio();\n"
        "    const playbackSpeed=await getPlaybackSpeedForComment();\n"
        "    activeAbort=new AbortController();",
    )

    old_source = (
        "const src=audioCtx.createBufferSource();\n"
        "      src.buffer=buffer;src.connect(gainNode);\n"
        "      if(next<audioCtx.currentTime+0.05)next=audioCtx.currentTime+0.08;\n"
        "      src.start(next);next+=buffer.duration;"
    )
    new_source = (
        "const src=audioCtx.createBufferSource();\n"
        "      src.buffer=buffer;src.playbackRate.value=playbackSpeed;src.connect(gainNode);\n"
        "      if(next<audioCtx.currentTime+0.05)next=audioCtx.currentTime+0.08;\n"
        "      src.start(next);next+=buffer.duration/playbackSpeed;"
    )
    html = html.replace(old_source, new_source)

    html = html.replace(
        "await loadVoices();await loadSettings();",
        "await loadVoices();await loadSettings();await loadPlaybackSpeed();",
    )

    volume_listener = (
        "$('volume').addEventListener('input',()=>{\n"
        "  if(gainNode)gainNode.gain.value=Number($('volume').value)/100;\n"
        "});"
    )
    speed_listener = r'''
$('playbackSpeed').addEventListener('input',()=>{
  const speed=Number($('playbackSpeed').value||1);
  $('playbackSpeedValue').textContent=speed.toFixed(2)+'x';
});
$('playbackSpeed').addEventListener('change',async()=>{
  try{
    const speed=await savePlaybackSpeed();
    $('settingsState').textContent=`✅ Tốc độ đọc đã lưu: ${speed.toFixed(2)}x`;
  }catch(e){
    $('settingsState').textContent='Lỗi lưu tốc độ: '+e;
  }
});'''
    if "$('playbackSpeed').addEventListener('input'" not in html:
        html = html.replace(volume_listener, volume_listener + "\n" + speed_listener)

    RUNTIME_WEB_FILE.write_text(html, encoding="utf-8")
    core.WEB_FILE = RUNTIME_WEB_FILE

    required_markers = (
        'id="playbackSpeed"',
        "getPlaybackSpeedForComment",
        "src.playbackRate.value=playbackSpeed",
        "buffer.duration/playbackSpeed",
    )
    missing = [marker for marker in required_markers if marker not in html]
    if missing:
        raise RuntimeError(f"Không chèn được UI tốc độ: thiếu {missing}")


_load_speed()
_patch_web()
core.SERVER_VERSION = "2.5"
core.app.version = "2.5"


if __name__ == "__main__":
    print(f"[SPEED] Playback speed: {_get_speed():.2f}x (0.70x - 1.50x)")
    print("[SPEED] Mỗi comment lấy tốc độ mới nhất trước khi phát.")
    uvicorn.run(core.app, host=core.HOST, port=core.PORT, log_level="info")
