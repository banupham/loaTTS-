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
TAIL_FADE_MS = max(5.0, min(120.0, float(os.getenv("LOA_TTS_TAIL_FADE_MS", "30"))))

RUNTIME_WEB_FILE = core.BASE_DIR / ".runtime_tailguard_index.html"

_original_comment_to_tts_text = core._comment_to_tts_text
_original_vieneu_factory = core.Vieneu


def _ensure_terminal_punctuation(text: str) -> str:
    value = str(text or "").strip()
    if value and value[-1] not in ".!?…":
        value += "."
    return value


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", str(text or "").strip()))


def _estimate_max_audio_seconds(text: str) -> float:
    estimated = TAIL_BASE_SECONDS + _word_count(text) * TAIL_PER_WORD_SECONDS
    return max(TAIL_MIN_SECONDS, min(TAIL_MAX_SECONDS, estimated))


def _fade_out(samples: np.ndarray, fade_samples: int) -> np.ndarray:
    output = np.asarray(samples, dtype=np.float32).reshape(-1).copy()
    count = min(max(1, int(fade_samples)), int(output.size))
    if count <= 1:
        if output.size:
            output[-1] = 0.0
        return output

    ramp = np.linspace(1.0, 0.0, count, endpoint=True, dtype=np.float32)
    output[-count:] *= ramp
    return output


def _patched_comment_to_tts_text(job: core.CommentJob) -> str:
    return _ensure_terminal_punctuation(_original_comment_to_tts_text(job))


class _TailGuardTTS:
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def infer_stream(self, text: str, *args, **kwargs):
        guarded_text = _ensure_terminal_punctuation(text)
        sample_rate = max(1, int(getattr(self._inner, "sample_rate", 48000) or 48000))
        max_seconds = _estimate_max_audio_seconds(guarded_text)
        max_samples = max(1, int(max_seconds * sample_rate))
        fade_samples = max(1, int(sample_rate * TAIL_FADE_MS / 1000.0))
        sent_samples = 0
        source = self._inner.infer_stream(guarded_text, *args, **kwargs)

        print(
            f"[TAIL-GUARD] text={guarded_text!r} | words={_word_count(guarded_text)} "
            f"| max={max_seconds:.2f}s | fade={TAIL_FADE_MS:.0f}ms"
        )

        try:
            for audio in source:
                arr = np.asarray(audio)
                sample_count = int(arr.size)
                if sample_count <= 0:
                    continue

                remaining = max_samples - sent_samples
                if remaining <= 0:
                    core.stats["tts_tail_guard_cuts"] = core.stats.get("tts_tail_guard_cuts", 0) + 1
                    print(
                        f"[TAIL-GUARD] CUT at {sent_samples / sample_rate:.2f}s "
                        f"(limit {max_seconds:.2f}s)"
                    )
                    break

                if sample_count >= remaining:
                    trimmed = arr.reshape(-1)[:remaining]
                    softened = _fade_out(trimmed, fade_samples)
                    if softened.size:
                        yield softened
                        sent_samples += int(softened.size)
                    core.stats["tts_tail_guard_cuts"] = core.stats.get("tts_tail_guard_cuts", 0) + 1
                    print(
                        f"[TAIL-GUARD] CUT+FADE at {sent_samples / sample_rate:.2f}s "
                        f"(limit {max_seconds:.2f}s, fade {TAIL_FADE_MS:.0f}ms)"
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


def _patched_vieneu_factory(*args, **kwargs):
    inner = _original_vieneu_factory(*args, **kwargs)
    infer_stream = getattr(inner, "infer_stream", None)
    if not callable(infer_stream):
        raise RuntimeError(
            "VieNeu runtime không có infer_stream; không thể bật Tail Guard."
        )
    print(f"[TAIL-GUARD] Đã bọc VieNeu runtime: {type(inner).__name__}")
    return _TailGuardTTS(inner)


def _patch_web_audio() -> None:
    html = core.WEB_FILE.read_text(encoding="utf-8")

    start_marker = "async function playComment(job){"
    end_marker = "\n\nfunction rulesToText"
    start = html.find(start_marker)
    end = html.find(end_marker, start)
    if start < 0 or end < 0:
        raise RuntimeError("Không tìm thấy playComment() để bật audio buffer ổn định.")

    play_comment = r'''async function playComment(job){
  stopLocal(false);
  const token=++playToken;
  currentJobId=job.id;
  try{
    await ensureAudio();
    const playbackSpeed=await getPlaybackSpeedForComment();
    activeAbort=new AbortController();
    setPlay(`⏳ ${job.display_name}: ${job.text}`);
    const r=await fetch(`/audio/${encodeURIComponent(job.id)}?device_id=${encodeURIComponent(deviceId)}`,{
      cache:'no-store',signal:activeAbort.signal
    });
    if(!r.ok)throw new Error(await r.text()||`HTTP ${r.status}`);
    if(!r.body)throw new Error('Trình duyệt không hỗ trợ streaming response');

    const sr=Number(r.headers.get('X-TTS-Sample-Rate')||48000);
    const fmt=(r.headers.get('X-TTS-Format')||'s16le').toLowerCase();
    if(fmt!=='s16le')throw new Error('Format audio không hỗ trợ: '+fmt);

    const reader=r.body.getReader();
    const blockSamples=Math.max(1,Math.floor(sr*0.12));
    const blockBytes=blockSamples*2;
    const startAhead=0.20;
    const minAhead=0.06;
    const recoverAhead=0.12;
    const edgeFadeSamples=Math.max(1,Math.floor(sr*0.008));

    let carry=new Uint8Array(0);
    let pending=new Uint8Array(0);
    let next=audioCtx.currentTime+startAhead;
    let first=true;
    let underruns=0;

    function scheduleBytes(bytes,isFinal=false){
      const n=bytes.length-bytes.length%2;
      if(!n)return;

      const count=n/2;
      const samples=new Float32Array(count);
      const view=new DataView(bytes.buffer,bytes.byteOffset,n);
      for(let i=0;i<count;i++)samples[i]=view.getInt16(i*2,true)/32768;

      const fadeCount=Math.min(edgeFadeSamples,count);
      if(first&&fadeCount>1){
        for(let i=0;i<fadeCount;i++)samples[i]*=i/(fadeCount-1);
      }
      if(isFinal&&fadeCount>1){
        for(let i=0;i<fadeCount;i++){
          const pos=count-fadeCount+i;
          samples[pos]*=(fadeCount-1-i)/(fadeCount-1);
        }
      }

      const buffer=audioCtx.createBuffer(1,count,sr);
      buffer.copyToChannel(samples,0);
      const src=audioCtx.createBufferSource();
      src.buffer=buffer;
      src.playbackRate.value=playbackSpeed;
      src.connect(gainNode);

      if(next<audioCtx.currentTime+minAhead){
        underruns++;
        next=audioCtx.currentTime+recoverAhead;
        console.warn(`[AUDIO] underrun #${underruns}; phục hồi buffer ${recoverAhead.toFixed(2)}s`);
      }

      src.start(next);
      next+=buffer.duration/playbackSpeed;
      activeSources.push(src);
      src.onended=()=>activeSources=activeSources.filter(x=>x!==src);

      if(first){
        first=false;
        wsSend({type:'started',job_id:job.id});
        setPlay(`🔊 ${job.display_name}: ${job.text}`);
      }
    }

    while(true){
      const {done,value}=await reader.read();
      if(done)break;
      if(token!==playToken)return;
      if(!value||!value.length)continue;

      const data=mergeBytes(carry,value);
      const n=data.length-data.length%2;
      carry=data.slice(n);
      if(n)pending=mergeBytes(pending,data.slice(0,n));

      while(pending.length>=blockBytes*2){
        scheduleBytes(pending.slice(0,blockBytes),false);
        pending=pending.slice(blockBytes);
      }
    }

    while(pending.length>blockBytes){
      scheduleBytes(pending.slice(0,blockBytes),false);
      pending=pending.slice(blockBytes);
    }
    if(pending.length>=2) scheduleBytes(pending,true);

    const remain=Math.max(0,next-audioCtx.currentTime);
    if(remain)await new Promise(ok=>setTimeout(ok,remain*1000));

    if(token===playToken){
      activeAbort=null;
      currentJobId=null;
      wsSend({type:'completed',job_id:job.id});
      setPlay(
        underruns
          ? `✅ Đọc xong · audio phục hồi ${underruns} lần.`
          : '✅ Đọc xong. Đang chờ comment tiếp theo...'
      );
    }
  }catch(e){
    if(e&&e.name==='AbortError')return;
    activeAbort=null;
    const id=currentJobId||job.id;
    currentJobId=null;
    wsSend({type:'failed',job_id:id,error:String(e)});
    setPlay('❌ Lỗi phát comment: '+e);
  }
}'''

    html = html[:start] + play_comment + html[end:]

    required = (
        "const blockSamples=Math.max(1,Math.floor(sr*0.12));",
        "const startAhead=0.20;",
        "while(pending.length>=blockBytes*2)",
        "scheduleBytes(pending,true)",
        "[AUDIO] underrun",
    )
    missing = [item for item in required if item not in html]
    if missing:
        raise RuntimeError(f"Không bật được audio buffer ổn định: thiếu {missing}")

    RUNTIME_WEB_FILE.write_text(html, encoding="utf-8")
    core.WEB_FILE = RUNTIME_WEB_FILE


core._comment_to_tts_text = _patched_comment_to_tts_text
core.Vieneu = _patched_vieneu_factory
core.stats.setdefault("tts_tail_guard_enabled", True)
core.stats.setdefault("tts_tail_guard_cuts", 0)
_patch_web_audio()
core.SERVER_VERSION = "2.8"
core.app.version = "2.8"


assert _ensure_terminal_punctuation("ok") == "ok."
assert _ensure_terminal_punctuation("ok!") == "ok!"
assert abs(_estimate_max_audio_seconds("ok.") - 2.2) < 1e-9
assert abs(_estimate_max_audio_seconds("chị ơi còn hàng không.") - 4.75) < 1e-9
assert _estimate_max_audio_seconds(" ".join(["a"] * 100)) == TAIL_MAX_SECONDS
_test_fade = _fade_out(np.ones(100, dtype=np.float32), 20)
assert _test_fade[-1] == 0.0
assert _test_fade[-20] > _test_fade[-2]


if __name__ == "__main__":
    print(
        "[TAIL-GUARD] ON | "
        f"base={TAIL_BASE_SECONDS:.2f}s per_word={TAIL_PER_WORD_SECONDS:.2f}s "
        f"min={TAIL_MIN_SECONDS:.2f}s max={TAIL_MAX_SECONDS:.2f}s "
        f"fade={TAIL_FADE_MS:.0f}ms"
    )
    print("[AUDIO] Stable stream: 120ms blocks | start buffer 200ms | edge fade 8ms.")
    print("[TAIL-GUARD] Tự thêm dấu kết câu và cắt mềm audio sinh dư trước comment kế tiếp.")
    uvicorn.run(core.app, host=core.HOST, port=core.PORT, log_level="info")
