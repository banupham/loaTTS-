from __future__ import annotations

import os
import re

import numpy as np
import uvicorn

import emoji_patch as runtime

core = runtime.core

# Tail guard defaults. They can be overridden from CMD before start.bat.
TAIL_BASE_SECONDS = max(0.0, float(os.getenv("LOA_TTS_TAIL_BASE", "1.5")))
TAIL_PER_WORD_SECONDS = max(0.05, float(os.getenv("LOA_TTS_TAIL_PER_WORD", "0.65")))
TAIL_MIN_SECONDS = max(0.5, float(os.getenv("LOA_TTS_TAIL_MIN", "2.2")))
TAIL_MAX_SECONDS = max(TAIL_MIN_SECONDS, float(os.getenv("LOA_TTS_TAIL_MAX", "15")))

_original_comment_to_tts_text = core._comment_to_tts_text
_original_infer_stream = core.Vieneu.infer_stream


def _ensure_terminal_punctuation(text: str) -> str:
    value = str(text or "").strip()
    if value and value[-1] not in ".!?…":
        value += "."
    return value


def _estimate_max_audio_seconds(text: str) -> float:
    words = re.findall(r"\S+", str(text or "").strip())
    estimated = TAIL_BASE_SECONDS + len(words) * TAIL_PER_WORD_SECONDS
    return max(TAIL_MIN_SECONDS, min(TAIL_MAX_SECONDS, estimated))


def _patched_comment_to_tts_text(job: core.CommentJob) -> str:
    return _ensure_terminal_punctuation(_original_comment_to_tts_text(job))


def _guarded_infer_stream(self, text: str, *args, **kwargs):
    guarded_text = _ensure_terminal_punctuation(text)
    sample_rate = max(1, int(getattr(self, "sample_rate", 48000) or 48000))
    max_seconds = _estimate_max_audio_seconds(guarded_text)
    max_samples = max(1, int(max_seconds * sample_rate))
    sent_samples = 0
    source = _original_infer_stream(self, guarded_text, *args, **kwargs)

    print(
        f"[TAIL-GUARD] text={guarded_text!r} | words={len(re.findall(r'\\S+', guarded_text))} "
        f"| max={max_seconds:.2f}s"
    )

    try:
        for audio in source:
            arr = np.asarray(audio)
            sample_count = int(arr.size)
            if sample_count <= 0:
                continue

            remaining = max_samples - sent_samples
            if remaining <= 0:
                print(
                    f"[TAIL-GUARD] CUT at {sent_samples / sample_rate:.2f}s "
                    f"(limit {max_seconds:.2f}s)"
                )
                break

            if sample_count > remaining:
                # Yield only the part that still fits the limit, then stop generation.
                trimmed = arr.reshape(-1)[:remaining]
                if trimmed.size:
                    yield trimmed
                    sent_samples += int(trimmed.size)
                print(
                    f"[TAIL-GUARD] CUT partial chunk at {sent_samples / sample_rate:.2f}s "
                    f"(limit {max_seconds:.2f}s)"
                )
                break

            yield audio
            sent_samples += sample_count
    finally:
        close = getattr(source, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


core._comment_to_tts_text = _patched_comment_to_tts_text
core.Vieneu.infer_stream = _guarded_infer_stream
core.stats.setdefault("tts_tail_guard_enabled", True)
core.SERVER_VERSION = "2.7"
core.app.version = "2.7"


# Startup guards for the duration formula and punctuation behavior.
assert _ensure_terminal_punctuation("ok") == "ok."
assert _ensure_terminal_punctuation("ok!") == "ok!"
assert abs(_estimate_max_audio_seconds("ok.") - 2.2) < 1e-9
assert abs(_estimate_max_audio_seconds("chị ơi còn hàng không.") - 4.75) < 1e-9
assert _estimate_max_audio_seconds(" ".join(["a"] * 100)) == TAIL_MAX_SECONDS


if __name__ == "__main__":
    print(
        "[TAIL-GUARD] ON | "
        f"base={TAIL_BASE_SECONDS:.2f}s per_word={TAIL_PER_WORD_SECONDS:.2f}s "
        f"min={TAIL_MIN_SECONDS:.2f}s max={TAIL_MAX_SECONDS:.2f}s"
    )
    print("[TAIL-GUARD] Tự thêm dấu kết câu và cắt audio sinh dư trước comment kế tiếp.")
    uvicorn.run(core.app, host=core.HOST, port=core.PORT, log_level="info")
