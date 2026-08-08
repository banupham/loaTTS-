from __future__ import annotations

import asyncio, json, os, re, sys, threading, time, uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from vieneu import Vieneu

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
WEB_FILE = BASE_DIR / "web" / "index.html"
SETTINGS_FILE = BASE_DIR / "settings.json"
HOST = os.getenv("LOA_TTS_HOST", "0.0.0.0")
PORT = int(os.getenv("LOA_TTS_PORT", "9000"))
EVENT_PATH = os.getenv("LOA_TTS_EVENT_PATH", "/tiktok-event").strip() or "/tiktok-event"
PRECISION = os.getenv("LOA_TTS_PRECISION", "int8").strip().lower()
THREADS = int(os.getenv("LOA_TTS_THREADS", "0"))
WARMUP = os.getenv("LOA_TTS_WARMUP", "1").strip().lower() not in {"0", "false", "no", "off"}
QUEUE_MAX = max(1, int(os.getenv("LOA_TTS_QUEUE_MAX", "30")))
COMMENT_MAX_AGE_SECONDS = max(0.0, float(os.getenv("LOA_TTS_COMMENT_MAX_AGE", "20")))
EVENT_ID_CACHE = max(100, int(os.getenv("LOA_TTS_EVENT_ID_CACHE", "10000")))
SERVER_VERSION = "2.1"
SERVER_INSTANCE_ID = uuid.uuid4().hex[:12]
SERVER_SESSION_TOKEN = os.getenv("GAME_EVENT_INSTANCE_TOKEN", "").strip()
SERVER_PID = os.getpid()

if PRECISION not in {"int8", "fp32"}:
    raise RuntimeError("LOA_TTS_PRECISION chỉ hỗ trợ int8 hoặc fp32")


class SpeakerSettings(BaseModel):
    voice: Optional[str] = None
    style: str = "tu_nhien"
    temperature: float = Field(default=0.78, ge=0.1, le=1.5)
    top_k: int = Field(default=25, ge=1, le=200)
    top_p: float = Field(default=0.93, gt=0.0, le=1.0)
    max_chars: int = Field(default=180, ge=50, le=1000)
    repetition_penalty: float = Field(default=1.2, ge=0.5, le=3.0)
    read_username: bool = False


class CommentJob(BaseModel):
    id: str
    event_id: str
    created_at: float
    display_name: str
    unique_id: Optional[str] = None
    text: str


tts: Optional[Vieneu] = None
tts_lock = threading.Lock()
settings_lock = threading.Lock()
settings = SpeakerSettings()

comment_queue: asyncio.Queue[CommentJob] = asyncio.Queue(maxsize=QUEUE_MAX)
speakers: dict[str, dict[str, Any]] = {}
active_speaker_id: Optional[str] = None
active_speaker_event = asyncio.Event()
state_lock = asyncio.Lock()
pending_completion: dict[str, asyncio.Future] = {}
worker_task: Optional[asyncio.Task] = None
current_job: Optional[CommentJob] = None
current_started_at: Optional[float] = None

seen_event_ids: set[str] = set()
seen_event_order: deque[str] = deque()
seen_event_lock = threading.Lock()

stats = {
    "webhook_requests": 0, "comments_received": 0, "comments_queued": 0,
    "comments_played": 0, "comments_failed": 0, "comments_dropped": 0,
    "comments_expired": 0, "comments_filtered_system": 0,
    "comments_cleaned_ui_prefix": 0, "ignored_non_comment": 0,
    "duplicate_events": 0, "last_webhook_at": None, "last_comment_at": None,
    "last_client_ip": None, "last_event_type": None, "last_health_handshake_at": None,
}

SYSTEM_COMMENT_PATTERNS = (
    re.compile(r"\bđã\s+chia\s+sẻ\s+(?:phiên\s+)?live\b", re.I),
    re.compile(r"\bđã\s+chia\s+se\s+(?:phiên\s+)?live\b", re.I),
    re.compile(r"\bshared\s+(?:the\s+)?live\b", re.I),
    re.compile(r"\bshared\s+(?:this\s+)?live\b", re.I),
)
LEADING_UI_NUMBER_RE = re.compile(r"^\s*(\d{1,3})\s+(?=\D)")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _save_settings() -> None:
    SETTINGS_FILE.write_text(json.dumps(settings.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")


def _load_settings() -> None:
    global settings
    if not SETTINGS_FILE.exists():
        _save_settings()
        return
    try:
        settings = SpeakerSettings.model_validate(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except Exception as exc:
        print(f"[SETTINGS] Không đọc được settings.json, dùng mặc định: {exc}")
        settings = SpeakerSettings()


def _register_event_id(event_id: str) -> bool:
    if not event_id:
        return True
    with seen_event_lock:
        if event_id in seen_event_ids:
            return False
        seen_event_ids.add(event_id)
        seen_event_order.append(event_id)
        while len(seen_event_order) > EVENT_ID_CACHE:
            seen_event_ids.discard(seen_event_order.popleft())
    return True


def _pcm16_bytes(audio: np.ndarray) -> bytes:
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if not audio.size:
        return b""
    if not np.isfinite(audio).all():
        audio = np.nan_to_num(audio, nan=0.0, posinf=1.0, neginf=-1.0)
    peak = float(np.max(np.abs(audio)))
    if peak > 1.0:
        audio = audio / peak * 0.98
    return (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2", copy=False).tobytes()


def _validate_voice(voice: Optional[str]) -> None:
    if not voice or tts is None:
        return
    try:
        tts.get_preset_voice(voice)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _normalize_comment_text(value: Any) -> tuple[str, bool]:
    text = str(value or "").replace("\u200b", " ").replace("\ufeff", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "", False
    cleaned = LEADING_UI_NUMBER_RE.sub("", text, count=1).strip()
    return cleaned, cleaned != text


def _is_system_comment(text: str) -> bool:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return bool(value) and any(p.search(value) for p in SYSTEM_COMMENT_PATTERNS)


def _comment_to_tts_text(job: CommentJob) -> str:
    with settings_lock:
        cfg = settings.model_copy()
    return f"{job.display_name} bình luận: {job.text}" if cfg.read_username and job.display_name else job.text


def _warmup_model() -> None:
    if not WARMUP or tts is None:
        return
    print("[TTS] Warm-up...")
    started = time.perf_counter()
    try:
        tts.infer(
            "Hệ thống đọc bình luận đã sẵn sàng.", voice=None, style="tu_nhien",
            temperature=0.72, top_k=20, top_p=0.90, max_chars=120,
            apply_watermark=False,
        )
        print(f"[TTS] Warm-up xong sau {time.perf_counter() - started:.2f}s")
    except Exception as exc:
        print(f"[TTS] Cảnh báo warm-up thất bại: {exc}")


async def _set_active_speaker(device_id: Optional[str]) -> None:
    global active_speaker_id
    async with state_lock:
        old_id = active_speaker_id
        active_speaker_id = device_id if device_id in speakers else None
        (active_speaker_event.set if active_speaker_id else active_speaker_event.clear)()
        if old_id and old_id != active_speaker_id and old_id in speakers:
            try:
                await speakers[old_id]["ws"].send_json({"type": "stop", "reason": "speaker_taken_over"})
            except Exception:
                pass
        if current_job and old_id and old_id != active_speaker_id:
            future = pending_completion.get(current_job.id)
            if future and not future.done():
                future.set_result(("retry", "active speaker changed"))


async def _wait_for_active_speaker() -> tuple[str, WebSocket]:
    while True:
        await active_speaker_event.wait()
        async with state_lock:
            if active_speaker_id and active_speaker_id in speakers:
                return active_speaker_id, speakers[active_speaker_id]["ws"]
            active_speaker_event.clear()


async def _queue_comment(job: CommentJob) -> None:
    if comment_queue.full():
        try:
            dropped = comment_queue.get_nowait()
            comment_queue.task_done()
            stats["comments_dropped"] += 1
            print(f"[QUEUE] Bỏ comment cũ vì queue đầy: {dropped.display_name}: {dropped.text[:80]}")
        except asyncio.QueueEmpty:
            pass
    await comment_queue.put(job)
    stats["comments_queued"] += 1


async def _speaker_worker() -> None:
    global current_job, current_started_at
    while True:
        await active_speaker_event.wait()
        job = await comment_queue.get()
        try:
            age = time.time() - job.created_at
            if COMMENT_MAX_AGE_SECONDS > 0 and age > COMMENT_MAX_AGE_SECONDS:
                stats["comments_expired"] += 1
                print(f"[QUEUE] Bỏ comment quá cũ ({age:.1f}s): {job.text[:80]}")
                continue

            while True:
                device_id, ws = await _wait_for_active_speaker()
                future = asyncio.get_running_loop().create_future()
                pending_completion[job.id] = future
                current_job, current_started_at = job, time.time()
                try:
                    await ws.send_json({"type": "comment", "job": {
                        "id": job.id, "event_id": job.event_id,
                        "display_name": job.display_name, "unique_id": job.unique_id,
                        "text": job.text,
                    }})
                except Exception as exc:
                    pending_completion.pop(job.id, None)
                    current_job, current_started_at = None, None
                    await _set_active_speaker(None)
                    print(f"[LOA] Không gửi được comment tới {device_id}: {exc}")
                    continue

                try:
                    result, detail = await asyncio.wait_for(
                        future, timeout=max(45.0, min(180.0, len(job.text) * 2.0))
                    )
                except asyncio.TimeoutError:
                    result, detail = "failed", "speaker timeout"
                finally:
                    pending_completion.pop(job.id, None)
                    current_job, current_started_at = None, None

                if result == "completed":
                    stats["comments_played"] += 1
                elif result == "retry":
                    if COMMENT_MAX_AGE_SECONDS <= 0 or time.time() - job.created_at <= COMMENT_MAX_AGE_SECONDS:
                        await _queue_comment(job)
                    else:
                        stats["comments_expired"] += 1
                else:
                    stats["comments_failed"] += 1
                    print(f"[LOA] Comment phát lỗi: {detail}")
                break
        finally:
            comment_queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global tts, worker_task
    print("=" * 70)
    print("LOA TTS TIKTOK COMMENT - SELF CONTAINED")
    print(f"Webhook : http://127.0.0.1:{PORT}{EVENT_PATH}")
    print(f"Web loa : http://127.0.0.1:{PORT}")
    print(f"Backend : onnx | precision={PRECISION} | threads={THREADS or 'auto'}")
    print("=" * 70)
    _load_settings()
    started = time.perf_counter()
    print("[TTS] Đang nạp VieNeu...")
    tts = Vieneu(backend="onnx", precision=PRECISION, threads=THREADS)
    print(f"[TTS] Nạp model xong sau {time.perf_counter() - started:.2f}s")
    _validate_voice(settings.voice)
    _warmup_model()
    worker_task = asyncio.create_task(_speaker_worker(), name="tiktok-comment-tts-worker")
    print("[READY] Chỉ đọc COMMENT; lọc chia sẻ LIVE và rác số UI đầu comment.")
    yield
    if worker_task:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
    tts = None


app = FastAPI(title="TikTok Comment TTS Speaker", version=SERVER_VERSION, lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def index():
    if not WEB_FILE.exists():
        raise HTTPException(status_code=500, detail="Thiếu web/index.html")
    return HTMLResponse(WEB_FILE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health(request: Request):
    is_handshake = request.headers.get("X-TikTok-Middleware-Handshake") == "1"
    client_ip = request.client.host if request.client else "unknown"
    if is_handshake:
        stats["last_health_handshake_at"], stats["last_client_ip"] = now_iso(), client_ip
        print(f"[HANDSHAKE] TikTok middleware -> loaTTS OK từ {client_ip}")
    return {
        "ok": True, "service": "game-event-server", "version": SERVER_VERSION,
        "instanceId": SERVER_INSTANCE_ID, "instanceToken": SERVER_SESSION_TOKEN,
        "pid": SERVER_PID, "eventPath": EVENT_PATH, "mode": "tiktok-comment-tts-only",
        "modelLoaded": tts is not None, "backend": "onnx", "precision": PRECISION,
        "queueSize": comment_queue.qsize(), "queueCapacity": QUEUE_MAX,
        "activeSpeakerId": active_speaker_id,
    }


@app.get("/status")
async def status():
    active = speakers.get(active_speaker_id) if active_speaker_id else None
    return {
        "ok": True, "mode": "comment-only", "model_loaded": tts is not None,
        "sample_rate": int(tts.sample_rate) if tts is not None else None,
        "precision": PRECISION, "queue_size": comment_queue.qsize(),
        "queue_capacity": QUEUE_MAX, "comment_max_age_seconds": COMMENT_MAX_AGE_SECONDS,
        "connected_speakers": len(speakers), "active_speaker_id": active_speaker_id,
        "active_speaker_name": active.get("name") if active else None,
        "current_comment": ({
            "display_name": current_job.display_name, "text": current_job.text,
            "started_at": current_started_at,
        } if current_job else None),
        "stats": stats,
    }


@app.get("/api/voices")
def voices():
    if tts is None:
        raise HTTPException(status_code=503, detail="Model chưa sẵn sàng")
    return [{"label": label, "id": voice_id} for label, voice_id in tts.list_preset_voices()]


@app.get("/api/settings")
def get_settings():
    with settings_lock:
        return settings.model_dump()


@app.post("/api/settings")
def update_settings(req: SpeakerSettings):
    global settings
    if req.style not in {"tu_nhien", "doc_truyen", "tin_tuc"}:
        raise HTTPException(status_code=400, detail="style không hợp lệ")
    _validate_voice(req.voice)
    with settings_lock:
        settings = req
        _save_settings()
        return {"ok": True, "settings": settings.model_dump()}


@app.post(EVENT_PATH)
async def tiktok_event(request: Request):
    stats["webhook_requests"] += 1
    stats["last_webhook_at"] = now_iso()
    stats["last_client_ip"] = request.client.host if request.client else "unknown"
    try:
        event = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"JSON không hợp lệ: {exc}") from exc
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="Event phải là JSON object")

    event_type = str(event.get("eventType") or "").strip().lower()
    event_id = str(event.get("eventId") or "").strip()
    stats["last_event_type"] = event_type or None
    if event_id and not _register_event_id(event_id):
        stats["duplicate_events"] += 1
        return {"ok": True, "duplicate": True, "eventId": event_id}
    if event_type != "comment":
        stats["ignored_non_comment"] += 1
        return {"ok": True, "ignored": True, "reason": "comment_only", "eventType": event_type or None}

    user, payload = event.get("user") or {}, event.get("payload") or {}
    text, removed_ui_number = _normalize_comment_text(payload.get("text"))
    if not text:
        return {"ok": True, "ignored": True, "reason": "empty_comment"}
    if _is_system_comment(text):
        stats["comments_filtered_system"] += 1
        print(f"[FILTER] Bỏ dòng hệ thống: {text}")
        return {"ok": True, "ignored": True, "reason": "system_share_message", "eventId": event_id or None}
    if removed_ui_number:
        stats["comments_cleaned_ui_prefix"] += 1
        print(f"[FILTER] Đã bỏ số UI đầu comment -> {text}")

    display_name = str(
        user.get("displayName") or user.get("uniqueId") or user.get("id") or "Viewer"
    ).strip()
    unique_raw = user.get("uniqueId")
    job = CommentJob(
        id=uuid.uuid4().hex[:12], event_id=event_id, created_at=time.time(),
        display_name=display_name, unique_id=str(unique_raw).strip() if unique_raw else None,
        text=text,
    )
    stats["comments_received"] += 1
    stats["last_comment_at"] = now_iso()
    await _queue_comment(job)
    print(f"[COMMENT] {display_name}: {text}")
    return {
        "ok": True, "accepted": True, "commentOnly": True,
        "eventId": event_id or None, "jobId": job.id,
        "queueSize": comment_queue.qsize(), "cleanedUiPrefix": removed_ui_number,
        "instanceId": SERVER_INSTANCE_ID, "pid": SERVER_PID,
    }


@app.post("/clear")
async def clear_queue():
    cleared = 0
    while True:
        try:
            comment_queue.get_nowait()
            comment_queue.task_done()
            cleared += 1
        except asyncio.QueueEmpty:
            break
    return {"ok": True, "cleared": cleared}


@app.post("/stop")
async def stop_current():
    if not current_job:
        return {"ok": True, "stopped": False}
    if active_speaker_id and active_speaker_id in speakers:
        try:
            await speakers[active_speaker_id]["ws"].send_json({"type": "stop", "reason": "user_stop"})
        except Exception:
            pass
    future = pending_completion.get(current_job.id)
    if future and not future.done():
        future.set_result(("failed", "stopped by user"))
    return {"ok": True, "stopped": True, "job_id": current_job.id}


@app.get("/audio/{job_id}")
def stream_comment_audio(job_id: str, device_id: str):
    if tts is None:
        raise HTTPException(status_code=503, detail="Model chưa sẵn sàng")
    if not current_job or current_job.id != job_id:
        raise HTTPException(status_code=404, detail="Comment không còn là job hiện tại")
    if not active_speaker_id or device_id != active_speaker_id:
        raise HTTPException(status_code=403, detail="Thiết bị này không phải loa chính")

    job = current_job
    text = _comment_to_tts_text(job)
    with settings_lock:
        cfg = settings.model_copy()
    _validate_voice(cfg.voice)
    sample_rate = int(tts.sample_rate)

    def body():
        started, first_audio = time.perf_counter(), None
        try:
            with tts_lock:
                for audio in tts.infer_stream(
                    text, voice=cfg.voice, style=cfg.style,
                    temperature=cfg.temperature, top_k=cfg.top_k, top_p=cfg.top_p,
                    max_chars=cfg.max_chars, repetition_penalty=cfg.repetition_penalty,
                    apply_watermark=False,
                ):
                    pcm = _pcm16_bytes(audio)
                    if not pcm:
                        continue
                    if first_audio is None:
                        first_audio = time.perf_counter()
                        print(
                            f"[TTS] First audio {first_audio - started:.3f}s | "
                            f"{job.display_name}: {job.text[:80]}"
                        )
                    yield pcm
        except GeneratorExit:
            print("[TTS] Browser ngắt stream.")
        except Exception as exc:
            print(f"[TTS] Stream error: {exc}")

    return StreamingResponse(
        body(), media_type=f"audio/pcm; rate={sample_rate}; channels=1",
        headers={
            "Cache-Control": "no-store", "X-TTS-Sample-Rate": str(sample_rate),
            "X-TTS-Channels": "1", "X-TTS-Format": "s16le",
        },
    )


@app.websocket("/ws/speaker")
async def speaker_socket(websocket: WebSocket):
    device_id = (websocket.query_params.get("device_id") or uuid.uuid4().hex[:10]).strip()[:64]
    name = (websocket.query_params.get("name") or "Loa TikTok").strip()[:80]
    await websocket.accept()
    async with state_lock:
        speakers[device_id] = {"ws": websocket, "name": name, "connected_at": time.time()}
    await websocket.send_json({
        "type": "hello", "device_id": device_id, "active": active_speaker_id == device_id,
        "message": "Đã kết nối. Bấm BẬT LOA TIKTOK để nhận comment.",
    })
    try:
        while True:
            message = await websocket.receive_json()
            msg_type = str(message.get("type") or "")
            if msg_type == "claim":
                await _set_active_speaker(device_id)
                await websocket.send_json({"type": "claimed", "device_id": device_id})
                print(f"[LOA] Loa chính: {name} ({device_id})")
            elif msg_type == "release":
                if active_speaker_id == device_id:
                    await _set_active_speaker(None)
                await websocket.send_json({"type": "released", "device_id": device_id})
            elif msg_type == "started":
                print(f"[LOA] Bắt đầu đọc {str(message.get('job_id') or '')} trên {name}")
            elif msg_type in {"completed", "failed"}:
                job_id = str(message.get("job_id") or "")
                future = pending_completion.get(job_id)
                if future and not future.done():
                    future.set_result(
                        ("completed", None) if msg_type == "completed"
                        else ("failed", str(message.get("error") or "speaker failed"))
                    )
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong", "time": time.time()})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        print(f"[LOA] WebSocket {device_id} error: {exc}")
    finally:
        was_active = active_speaker_id == device_id
        async with state_lock:
            speakers.pop(device_id, None)
        if was_active:
            await _set_active_speaker(None)
            if current_job:
                future = pending_completion.get(current_job.id)
                if future and not future.done():
                    future.set_result(("retry", "speaker disconnected"))
        print(f"[LOA] Mất kết nối: {name} ({device_id})")


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
