from __future__ import annotations

import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import uvicorn
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

import stable_runtime as runtime

core = runtime.core

QUEUE_MAX = max(1, int(os.getenv("LOA_TTS_QUEUE_MAX", "10")))
COMMENT_MAX_AGE_SECONDS = max(
    0.0, float(os.getenv("LOA_TTS_COMMENT_MAX_AGE", "8"))
)
COMMENT_DELAY_SECONDS = max(
    0.0, float(os.getenv("LOA_TTS_COMMENT_DELAY", "1.0"))
)
CLARITY_PRESET_ENABLED = os.getenv(
    "LOA_TTS_CLARITY_PRESET", "1"
).strip().lower() not in {"0", "false", "no", "off"}

# Chỉ giữ đúng một audio đã chuẩn bị phía trước audio đang phát.
ready_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
ready_slot = asyncio.Semaphore(1)
prepared_audio: dict[str, bytes] = {}
prepared_audio_lock = threading.Lock()
tts_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts-prebuffer")
generator_task: asyncio.Task | None = None

CLARITY_MARKER = core.BASE_DIR / ".clarity_preset_v1"

# Queue nhận comment vẫn tối đa 10. Khi quá tải, core._queue_comment giữ cơ chế
# bỏ comment cũ nhất lúc queue đầy. Generator phía dưới mỗi chu kỳ chỉ chọn
# một comment cũ nhất còn lại rồi bỏ phần còn lại của burst đó.
core.QUEUE_MAX = QUEUE_MAX
core.COMMENT_MAX_AGE_SECONDS = COMMENT_MAX_AGE_SECONDS
core.comment_queue = asyncio.Queue(maxsize=QUEUE_MAX)
core.stats.setdefault("pipeline_generated", 0)
core.stats.setdefault("pipeline_skipped_burst", 0)
core.stats.setdefault("pipeline_prepared_bytes", 0)
core.stats.setdefault("pipeline_play_delays", 0)


def _is_expired(job) -> tuple[bool, float]:
    age = time.time() - job.created_at
    return (
        COMMENT_MAX_AGE_SECONDS > 0 and age > COMMENT_MAX_AGE_SECONDS,
        age,
    )


def _drop_rest_of_burst() -> int:
    dropped = 0
    while True:
        try:
            skipped = core.comment_queue.get_nowait()
            core.comment_queue.task_done()
            dropped += 1
            print(
                f"[PIPE] Bỏ comment cùng burst: "
                f"{skipped.display_name}: {skipped.text[:80]}"
            )
        except asyncio.QueueEmpty:
            break
    if dropped:
        core.stats["pipeline_skipped_burst"] += dropped
        core.stats["comments_dropped"] += dropped
    return dropped


def _render_job_sync(job) -> bytes:
    if core.tts is None:
        raise RuntimeError("Model chưa sẵn sàng")

    text = core._comment_to_tts_text(job)
    with core.settings_lock:
        cfg = core.settings.model_copy(deep=True)
    core._validate_voice(cfg.voice)

    pcm_all = bytearray()
    started = time.perf_counter()
    chunk_count = 0

    # Chỉ một thread duy nhất được sinh TTS. Không phát trực tiếp trong lúc sinh.
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

    elapsed = time.perf_counter() - started
    print(
        f"[GEN] Xong {job.id} | {elapsed:.2f}s | chunks={chunk_count} "
        f"| pcm={len(pcm_all) / 1024:.1f} KiB | {job.display_name}: {job.text[:80]}"
    )
    return bytes(pcm_all)


async def _generation_worker() -> None:
    loop = asyncio.get_running_loop()

    while True:
        # Chỉ chuẩn bị đúng 1 comment phía trước comment đang phát.
        await ready_slot.acquire()
        slot_owned = True
        job = None

        try:
            job = await core.comment_queue.get()
            try:
                expired, age = _is_expired(job)
                if expired:
                    core.stats["comments_expired"] += 1
                    print(
                        f"[GEN] Bỏ comment quá cũ ({age:.1f}s): "
                        f"{job.display_name}: {job.text[:80]}"
                    )
                    continue

                # Một burst có thể chứa tối đa 10 comment. Chọn FIFO cũ nhất;
                # các comment còn lại trong burst bị bỏ để không tạo backlog.
                _drop_rest_of_burst()

                pcm = await loop.run_in_executor(tts_executor, _render_job_sync, job)
                if not pcm:
                    core.stats["comments_failed"] += 1
                    print(f"[GEN] Không tạo được audio: {job.id}")
                    continue

                with prepared_audio_lock:
                    prepared_audio[job.id] = pcm

                core.stats["pipeline_generated"] += 1
                core.stats["pipeline_prepared_bytes"] += len(pcm)
                await ready_queue.put(job)
                slot_owned = False  # playback worker sẽ release khi lấy job
            finally:
                core.comment_queue.task_done()
        finally:
            if slot_owned:
                ready_slot.release()


async def _speaker_worker_pipeline() -> None:
    while True:
        await core.active_speaker_event.wait()
        job = await ready_queue.get()
        ready_slot.release()  # cho generator chuẩn bị comment kế tiếp khi đang phát job này

        try:
            expired, age = _is_expired(job)
            if expired:
                core.stats["comments_expired"] += 1
                print(
                    f"[PLAY] Bỏ audio đã cũ ({age:.1f}s): "
                    f"{job.display_name}: {job.text[:80]}"
                )
                continue

            # Giữ khoảng nghỉ rõ ràng giữa các comment. Audio đã được tạo sẵn,
            # nên khoảng nghỉ này không làm browser phải chờ TTS sinh chunk.
            if COMMENT_DELAY_SECONDS > 0:
                core.stats["pipeline_play_delays"] += 1
                print(
                    f"[PLAY] Nghỉ {COMMENT_DELAY_SECONDS:.2f}s trước khi phát: "
                    f"{job.display_name}: {job.text[:80]}"
                )
                await asyncio.sleep(COMMENT_DELAY_SECONDS)

            expired, age = _is_expired(job)
            if expired:
                core.stats["comments_expired"] += 1
                print(
                    f"[PLAY] Bỏ sau khoảng nghỉ vì đã cũ ({age:.1f}s): "
                    f"{job.display_name}: {job.text[:80]}"
                )
                continue

            while True:
                device_id, ws = await core._wait_for_active_speaker()
                future = asyncio.get_running_loop().create_future()
                core.pending_completion[job.id] = future
                core.current_job = job
                core.current_started_at = time.time()

                try:
                    await ws.send_json(
                        {
                            "type": "comment",
                            "job": {
                                "id": job.id,
                                "event_id": job.event_id,
                                "display_name": job.display_name,
                                "unique_id": job.unique_id,
                                "text": job.text,
                            },
                        }
                    )
                except Exception as exc:
                    core.pending_completion.pop(job.id, None)
                    core.current_job = None
                    core.current_started_at = None
                    await core._set_active_speaker(None)
                    print(f"[PLAY] Không gửi được comment tới {device_id}: {exc}")
                    continue

                try:
                    result, detail = await asyncio.wait_for(
                        future,
                        timeout=max(45.0, min(180.0, len(job.text) * 2.0)),
                    )
                except asyncio.TimeoutError:
                    result, detail = "failed", "speaker timeout"
                finally:
                    core.pending_completion.pop(job.id, None)
                    core.current_job = None
                    core.current_started_at = None

                if result == "completed":
                    core.stats["comments_played"] += 1
                elif result == "retry":
                    expired, _ = _is_expired(job)
                    if not expired:
                        # Audio đã có sẵn, giữ cache để phát lại trên loa mới.
                        await ready_slot.acquire()
                        await ready_queue.put(job)
                    else:
                        core.stats["comments_expired"] += 1
                else:
                    core.stats["comments_failed"] += 1
                    print(f"[PLAY] Comment phát lỗi: {detail}")
                break
        finally:
            # Nếu job không được đưa lại ready_queue thì dọn PCM khỏi RAM.
            queued_again = any(item is job for item in list(ready_queue._queue))
            if not queued_again:
                with prepared_audio_lock:
                    prepared_audio.pop(job.id, None)
            ready_queue.task_done()


def _prepared_audio_endpoint(job_id: str, device_id: str):
    if core.tts is None:
        raise HTTPException(status_code=503, detail="Model chưa sẵn sàng")
    if not core.current_job or core.current_job.id != job_id:
        raise HTTPException(status_code=404, detail="Comment không còn là job hiện tại")
    if not core.active_speaker_id or device_id != core.active_speaker_id:
        raise HTTPException(status_code=403, detail="Thiết bị này không phải loa chính")

    with prepared_audio_lock:
        pcm = prepared_audio.get(job_id)
    if pcm is None:
        raise HTTPException(status_code=409, detail="Audio chưa được chuẩn bị")

    sample_rate = int(core.tts.sample_rate)
    block = 64 * 1024

    def body():
        # Luồng phát chỉ truyền bytes đã tạo xong; tuyệt đối không gọi TTS ở đây.
        for pos in range(0, len(pcm), block):
            yield pcm[pos : pos + block]

    return StreamingResponse(
        body(),
        media_type=f"audio/pcm; rate={sample_rate}; channels=1",
        headers={
            "Cache-Control": "no-store",
            "X-TTS-Sample-Rate": str(sample_rate),
            "X-TTS-Channels": "1",
            "X-TTS-Format": "s16le",
            "X-TTS-Prepared": "1",
            "X-TTS-PCM-Bytes": str(len(pcm)),
        },
    )


def _patch_audio_route() -> None:
    for route in core.app.routes:
        if getattr(route, "path", None) != "/audio/{job_id}":
            continue
        route.endpoint = _prepared_audio_endpoint
        dependant = getattr(route, "dependant", None)
        if dependant is not None:
            dependant.call = _prepared_audio_endpoint
        print("[PIPE] /audio dùng PCM đã tạo sẵn; không sinh TTS trong HTTP stream.")
        return
    raise RuntimeError("Không tìm thấy route /audio/{job_id}")


def _apply_clarity_preset_once() -> None:
    if not CLARITY_PRESET_ENABLED or CLARITY_MARKER.exists():
        return

    with core.settings_lock:
        core.settings = core.settings.model_copy(
            update={
                "style": "tu_nhien",
                "temperature": 0.62,
                "top_k": 20,
                "top_p": 0.90,
                "max_chars": 160,
                "repetition_penalty": 1.10,
            }
        )
        core._save_settings()

    CLARITY_MARKER.write_text(
        "style=tu_nhien\ntemperature=0.62\ntop_k=20\ntop_p=0.90\n"
        "max_chars=160\nrepetition_penalty=1.10\n",
        encoding="utf-8",
    )
    print(
        "[VOICE] Đã áp preset rõ chữ: natural, temp=0.62, top_k=20, "
        "top_p=0.90, repetition=1.10, max_chars=160"
    )


# Original lifespan sẽ tạo core.worker_task bằng hàm _speaker_worker hiện tại.
# Patch trước khi uvicorn khởi động để task đó trở thành playback worker.
core._speaker_worker = _speaker_worker_pipeline
_patch_audio_route()

_original_lifespan_context = core.app.router.lifespan_context


@asynccontextmanager
async def _pipeline_lifespan(app):
    global generator_task
    async with _original_lifespan_context(app):
        _apply_clarity_preset_once()
        generator_task = asyncio.create_task(
            _generation_worker(), name="tiktok-comment-tts-generator"
        )
        try:
            yield
        finally:
            if generator_task:
                generator_task.cancel()
                try:
                    await generator_task
                except asyncio.CancelledError:
                    pass
                generator_task = None
            tts_executor.shutdown(wait=False, cancel_futures=True)


core.app.router.lifespan_context = _pipeline_lifespan
core.SERVER_VERSION = "3.2"
core.app.version = "3.2"


if __name__ == "__main__":
    print("[CORE] v3.2 - TWO-STAGE PREBUFFER PIPELINE")
    print(
        f"[PIPE] queue={QUEUE_MAX} | ready_audio=1 | max_age={COMMENT_MAX_AGE_SECONDS:.1f}s "
        f"| gap={COMMENT_DELAY_SECONDS:.2f}s"
    )
    print(f"[PIPE] TTS generation threads=1 | ONNX threads={core.THREADS or 'auto'}")
    print("[PIPE] Generator: chọn comment cũ nhất, bỏ phần còn lại của burst.")
    print("[PIPE] Player: chỉ truyền PCM đã tạo hoàn chỉnh.")
    uvicorn.run(core.app, host=core.HOST, port=core.PORT, log_level="info")
