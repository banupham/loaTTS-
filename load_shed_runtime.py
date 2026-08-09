from __future__ import annotations

import asyncio
import os
import time

import uvicorn

import stable_runtime as runtime

core = runtime.core

QUEUE_MAX = max(1, int(os.getenv("LOA_TTS_QUEUE_MAX", "10")))
COMMENT_MAX_AGE_SECONDS = max(
    0.0, float(os.getenv("LOA_TTS_COMMENT_MAX_AGE", "8"))
)
COMMENT_START_DELAY_SECONDS = max(
    0.0, float(os.getenv("LOA_TTS_COMMENT_DELAY", "1.0"))
)

# Thay queue trước khi FastAPI lifespan khởi động. Không đụng vào /audio,
# VieNeu.infer_stream() hoặc Web Audio player.
core.QUEUE_MAX = QUEUE_MAX
core.COMMENT_MAX_AGE_SECONDS = COMMENT_MAX_AGE_SECONDS
core.comment_queue = asyncio.Queue(maxsize=QUEUE_MAX)
core.stats.setdefault("load_shed_delays", 0)


def _is_expired(job) -> tuple[bool, float]:
    age = time.time() - job.created_at
    expired = COMMENT_MAX_AGE_SECONDS > 0 and age > COMMENT_MAX_AGE_SECONDS
    return expired, age


async def _speaker_worker_load_shed() -> None:
    global_current = core

    while True:
        await global_current.active_speaker_event.wait()
        job = await global_current.comment_queue.get()

        try:
            expired, age = _is_expired(job)
            if expired:
                global_current.stats["comments_expired"] += 1
                print(
                    f"[LOAD] Bỏ comment quá cũ ({age:.1f}s): "
                    f"{job.display_name}: {job.text[:80]}"
                )
                continue

            # Không phát ngay khi vừa lấy comment khỏi queue. Khoảng nghỉ này
            # tách rõ hai phiên TTS và cho CPU/browser/stream có thời gian hồi.
            if COMMENT_START_DELAY_SECONDS > 0:
                global_current.stats["load_shed_delays"] += 1
                print(
                    f"[LOAD] Chờ {COMMENT_START_DELAY_SECONDS:.2f}s trước khi đọc: "
                    f"{job.display_name}: {job.text[:80]}"
                )
                await asyncio.sleep(COMMENT_START_DELAY_SECONDS)

                # Trong lúc chờ, comment có thể đã quá cũ. Bỏ luôn thay vì
                # ép TTS chạy backlog đã không còn giá trị trong LIVE.
                expired, age = _is_expired(job)
                if expired:
                    global_current.stats["comments_expired"] += 1
                    print(
                        f"[LOAD] Bỏ comment sau thời gian chờ vì đã cũ ({age:.1f}s): "
                        f"{job.display_name}: {job.text[:80]}"
                    )
                    continue

            while True:
                device_id, ws = await global_current._wait_for_active_speaker()
                future = asyncio.get_running_loop().create_future()
                global_current.pending_completion[job.id] = future
                global_current.current_job = job
                global_current.current_started_at = time.time()

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
                    global_current.pending_completion.pop(job.id, None)
                    global_current.current_job = None
                    global_current.current_started_at = None
                    await global_current._set_active_speaker(None)
                    print(
                        f"[LOA] Không gửi được comment tới {device_id}: {exc}"
                    )
                    continue

                try:
                    result, detail = await asyncio.wait_for(
                        future,
                        timeout=max(45.0, min(180.0, len(job.text) * 2.0)),
                    )
                except asyncio.TimeoutError:
                    result, detail = "failed", "speaker timeout"
                finally:
                    global_current.pending_completion.pop(job.id, None)
                    global_current.current_job = None
                    global_current.current_started_at = None

                if result == "completed":
                    global_current.stats["comments_played"] += 1
                elif result == "retry":
                    expired, _ = _is_expired(job)
                    if not expired:
                        await global_current._queue_comment(job)
                    else:
                        global_current.stats["comments_expired"] += 1
                else:
                    global_current.stats["comments_failed"] += 1
                    print(f"[LOA] Comment phát lỗi: {detail}")
                break
        finally:
            global_current.comment_queue.task_done()


core._speaker_worker = _speaker_worker_load_shed
core.SERVER_VERSION = "3.1"
core.app.version = "3.1"


if __name__ == "__main__":
    print("[CORE] Stable audio: VieNeu infer_stream + PCM player nguyên bản")
    print(
        f"[LOAD] queue={QUEUE_MAX} | max_age={COMMENT_MAX_AGE_SECONDS:.1f}s "
        f"| delay_before_play={COMMENT_START_DELAY_SECONDS:.2f}s"
    )
    print(f"[LOAD] ONNX threads={core.THREADS or 'auto'}")
    print("[LOAD] Mỗi comment chờ trước khi phát; queue đầy bỏ comment cũ nhất.")
    uvicorn.run(core.app, host=core.HOST, port=core.PORT, log_level="info")
