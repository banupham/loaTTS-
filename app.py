import asyncio
import itertools
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
WEB_FILE = BASE_DIR / "web" / "index.html"

HOST = os.getenv("LOA_TTS_HOST", "0.0.0.0")
PORT = int(os.getenv("LOA_TTS_PORT", "8780"))
TTS_SERVER = os.getenv("LOA_TTS_SERVER", "http://127.0.0.1:8765").rstrip("/")
MAX_RETRIES = int(os.getenv("LOA_TTS_MAX_RETRIES", "2"))


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    voice: Optional[str] = None
    style: str = "tu_nhien"
    priority: int = Field(default=50, ge=0, le=1000)
    temperature: float = Field(default=0.78, ge=0.1, le=1.5)
    top_k: int = Field(default=25, ge=1, le=200)
    top_p: float = Field(default=0.93, gt=0.0, le=1.0)
    max_chars: int = Field(default=180, ge=50, le=1000)
    repetition_penalty: float = Field(default=1.2, ge=0.5, le=3.0)
    apply_watermark: bool = False


class QueueJob(BaseModel):
    id: str
    created_at: float
    retries: int = 0
    request: SpeakRequest


queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
sequence = itertools.count()
speakers: dict[str, dict] = {}
active_speaker_id: Optional[str] = None
active_speaker_event = asyncio.Event()
state_lock = asyncio.Lock()
pending_completion: dict[str, asyncio.Future] = {}
worker_task: Optional[asyncio.Task] = None
current_job: Optional[QueueJob] = None
current_started_at: Optional[float] = None
played_count = 0
failed_count = 0


def _job_payload(job: QueueJob) -> dict:
    data = job.request.model_dump()
    data["id"] = job.id
    data["created_at"] = job.created_at
    data["retries"] = job.retries
    return data


async def _set_active_speaker(device_id: Optional[str]) -> None:
    global active_speaker_id
    async with state_lock:
        old_id = active_speaker_id
        active_speaker_id = device_id if device_id in speakers else None
        if active_speaker_id:
            active_speaker_event.set()
        else:
            active_speaker_event.clear()

        if old_id and old_id != active_speaker_id and old_id in speakers:
            try:
                await speakers[old_id]["ws"].send_json(
                    {"type": "stop", "reason": "speaker_taken_over"}
                )
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
            device_id = active_speaker_id
            if device_id and device_id in speakers:
                return device_id, speakers[device_id]["ws"]
            active_speaker_event.clear()


async def _speaker_worker() -> None:
    global current_job, current_started_at, played_count, failed_count

    while True:
        priority, _, job = await queue.get()
        try:
            while True:
                device_id, ws = await _wait_for_active_speaker()
                loop = asyncio.get_running_loop()
                future = loop.create_future()
                pending_completion[job.id] = future
                current_job = job
                current_started_at = time.time()

                try:
                    await ws.send_json({"type": "speak", "job": _job_payload(job)})
                except Exception as exc:
                    pending_completion.pop(job.id, None)
                    current_job = None
                    current_started_at = None
                    await _set_active_speaker(None)
                    print(f"[LOA] Không gửi được job {job.id} tới {device_id}: {exc}")
                    continue

                timeout_seconds = max(60.0, min(900.0, len(job.request.text) * 2.5))
                try:
                    result, detail = await asyncio.wait_for(future, timeout=timeout_seconds)
                except asyncio.TimeoutError:
                    result, detail = "retry", "speaker timeout"
                finally:
                    pending_completion.pop(job.id, None)
                    current_job = None
                    current_started_at = None

                if result == "completed":
                    played_count += 1
                    break

                if result == "cancelled":
                    break

                failed_count += 1
                if job.retries < MAX_RETRIES:
                    job.retries += 1
                    print(
                        f"[LOA] Phát job {job.id} lỗi ({detail}), thử lại "
                        f"{job.retries}/{MAX_RETRIES}"
                    )
                    await queue.put((priority, next(sequence), job))
                else:
                    print(f"[LOA] Bỏ job {job.id} sau {job.retries} lần thử lại: {detail}")
                break
        finally:
            queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker_task
    worker_task = asyncio.create_task(_speaker_worker(), name="loa-tts-worker")
    print("=" * 66)
    print("LOA TTS THUONG TRUC")
    print(f"Web/API : http://127.0.0.1:{PORT}")
    print(f"TTS     : {TTS_SERVER}")
    print("=" * 66)
    yield
    if worker_task:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Loa TTS Thuong Truc", version="1.0.0", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def index():
    if not WEB_FILE.exists():
        raise HTTPException(status_code=500, detail="Thiếu web/index.html")
    return HTMLResponse(WEB_FILE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@app.get("/status")
async def status():
    active = speakers.get(active_speaker_id) if active_speaker_id else None
    return {
        "status": "ok",
        "tts_server": TTS_SERVER,
        "queue_size": queue.qsize(),
        "connected_speakers": len(speakers),
        "active_speaker_id": active_speaker_id,
        "active_speaker_name": active.get("name") if active else None,
        "current_job_id": current_job.id if current_job else None,
        "current_text": current_job.request.text if current_job else None,
        "current_started_at": current_started_at,
        "played": played_count,
        "failed": failed_count,
    }


@app.get("/api/tts-health")
def tts_health():
    try:
        response = requests.get(TTS_SERVER + "/health", timeout=5)
        data = response.json()
        if not response.ok:
            raise HTTPException(status_code=response.status_code, detail=data)
        return data
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail=f"Không kết nối được TTS server: {exc}") from exc


@app.get("/api/voices")
def voices():
    try:
        response = requests.get(TTS_SERVER + "/voices", timeout=10)
        data = response.json()
        if not response.ok:
            raise HTTPException(status_code=response.status_code, detail=data)
        return data
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail=f"Không kết nối được TTS server: {exc}") from exc


@app.post("/speak")
async def speak(req: SpeakRequest):
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text không được rỗng")
    req.text = text
    job = QueueJob(id=uuid.uuid4().hex[:12], created_at=time.time(), request=req)
    await queue.put((req.priority, next(sequence), job))
    return {
        "ok": True,
        "id": job.id,
        "priority": req.priority,
        "queue_size": queue.qsize(),
        "active_speaker": active_speaker_id,
    }


@app.post("/clear")
async def clear_queue():
    cleared = 0
    while True:
        try:
            queue.get_nowait()
            queue.task_done()
            cleared += 1
        except asyncio.QueueEmpty:
            break
    return {"ok": True, "cleared": cleared, "current_job_stopped": False}


@app.post("/stop")
async def stop_current():
    if not current_job:
        return {"ok": True, "stopped": False}

    if active_speaker_id and active_speaker_id in speakers:
        try:
            await speakers[active_speaker_id]["ws"].send_json(
                {"type": "stop", "reason": "api_stop"}
            )
        except Exception:
            pass

    future = pending_completion.get(current_job.id)
    if future and not future.done():
        future.set_result(("cancelled", "stopped by API"))
    return {"ok": True, "stopped": True, "job_id": current_job.id}


@app.post("/api/stream")
def stream_to_speaker(req: SpeakRequest):
    payload = req.model_dump()
    payload.pop("priority", None)

    try:
        upstream = requests.post(
            TTS_SERVER + "/tts/stream",
            json=payload,
            stream=True,
            timeout=(10, 900),
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail=f"Không kết nối được TTS stream: {exc}") from exc

    if not upstream.ok:
        try:
            detail = upstream.json()
        except Exception:
            detail = upstream.text or f"HTTP {upstream.status_code}"
        status_code = upstream.status_code
        upstream.close()
        raise HTTPException(status_code=status_code, detail=detail)

    sample_rate = upstream.headers.get("X-TTS-Sample-Rate", "48000")
    channels = upstream.headers.get("X-TTS-Channels", "1")
    audio_format = upstream.headers.get("X-TTS-Format", "s16le")

    def body():
        try:
            for chunk in upstream.iter_content(chunk_size=4096):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return StreamingResponse(
        body(),
        media_type=f"audio/pcm; rate={sample_rate}; channels={channels}",
        headers={
            "Cache-Control": "no-store",
            "X-TTS-Sample-Rate": sample_rate,
            "X-TTS-Channels": channels,
            "X-TTS-Format": audio_format,
        },
    )


@app.websocket("/ws/speaker")
async def speaker_socket(websocket: WebSocket):
    global active_speaker_id

    device_id = (websocket.query_params.get("device_id") or uuid.uuid4().hex[:10]).strip()[:64]
    name = (websocket.query_params.get("name") or "Loa web").strip()[:80]
    await websocket.accept()

    async with state_lock:
        speakers[device_id] = {
            "ws": websocket,
            "name": name,
            "connected_at": time.time(),
        }

    await websocket.send_json(
        {
            "type": "hello",
            "device_id": device_id,
            "active": active_speaker_id == device_id,
            "message": "Đã kết nối. Bấm BẬT LOA để nhận quyền phát.",
        }
    )

    try:
        while True:
            message = await websocket.receive_json()
            msg_type = message.get("type")

            if msg_type == "claim":
                await _set_active_speaker(device_id)
                await websocket.send_json({"type": "claimed", "device_id": device_id})
                print(f"[LOA] Active speaker: {name} ({device_id})")

            elif msg_type == "release":
                if active_speaker_id == device_id:
                    await _set_active_speaker(None)
                await websocket.send_json({"type": "released", "device_id": device_id})

            elif msg_type in {"started", "completed", "failed"}:
                job_id = str(message.get("job_id") or "")
                if msg_type == "started":
                    print(f"[LOA] Started {job_id} on {name}")
                    continue

                future = pending_completion.get(job_id)
                if future and not future.done():
                    if msg_type == "completed":
                        future.set_result(("completed", None))
                    else:
                        future.set_result(("retry", str(message.get("error") or "speaker failed")))

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
        print(f"[LOA] Speaker disconnected: {name} ({device_id})")


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
