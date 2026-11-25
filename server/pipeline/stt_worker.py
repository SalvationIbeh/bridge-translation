"""
STT Workers — Faster-Whisper pool draining the STT queue.
N worker coroutines run concurrently, each with their own model instance.
Completed jobs are forwarded to the translation queue.
"""
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from faster_whisper import WhisperModel

import config
from pipeline.job import Job

logger = logging.getLogger(__name__)

# Thread pool for CPU-bound whisper inference (keeps asyncio loop unblocked)
_executor = ThreadPoolExecutor(max_workers=config.STT_WORKERS, thread_name_prefix="stt")


def _load_whisper_instance(worker_id: int) -> WhisperModel:
    logger.info(f"Loading Whisper instance {worker_id} on {config.STT_DEVICE}...")
    parts = config.STT_DEVICE.split(":")
    model = WhisperModel(
        config.WHISPER_MODEL,
        device=parts[0],
        device_index=int(parts[1]) if len(parts) > 1 else 0,
        compute_type=config.WHISPER_COMPUTE,
    )
    logger.info(f"Whisper instance {worker_id} ready.")
    return model


# Known Whisper hallucinations on non-speech (it was trained on scraped subtitles,
# so on silence/noise it emits these). Matched case-insensitively as substrings of a
# segment. Defense-in-depth behind the no_speech/logprob gate below.
_HALLUCINATION_MARKERS = (
    "amara.org",
    "subtítulos",
    "subscribe",
    "suscríbete",
    "thanks for watching",
    "gracias por ver",
)


def _is_repetition_loop(low: str) -> bool:
    """True if the text is a short phrase repeated end-to-end (Whisper loop on
    marginal audio), e.g. "okay okay okay okay" or "it's okay it's okay it's okay".
    Tokens are punctuation-stripped so trailing ./, don't break the cycle."""
    toks = [t.strip(".,!?¡¿;:") for t in low.split()]
    toks = [t for t in toks if t]
    n = len(toks)
    if n < 3:
        return False
    # Smallest repeating unit of 1–3 words that tiles the whole utterance ≥3×.
    for unit in (1, 2, 3):
        if n >= unit * 3 and n % unit == 0:
            head = toks[:unit]
            if all(toks[i * unit:(i + 1) * unit] == head for i in range(n // unit)):
                return True
    return False


def _is_hallucinated(text: str, no_speech_prob: float, avg_logprob: float) -> bool:
    """True if a Whisper segment looks invented rather than spoken."""
    low = text.lower().strip()
    if not low:
        return True
    # Low-confidence speech over a window Whisper itself thinks is silence.
    if no_speech_prob >= config.STT_NO_SPEECH_THRESHOLD and avg_logprob < config.STT_LOGPROB_THRESHOLD:
        return True
    if any(m in low for m in _HALLUCINATION_MARKERS):
        return True
    if _is_repetition_loop(low):
        return True
    return False


def _transcribe(model: WhisperModel, audio_bytes: bytes, language: str) -> str:
    """
    Blocking transcription — runs in ThreadPoolExecutor.
    Converts raw PCM bytes → float32 numpy array → Whisper segments.
    """
    import io
    import numpy as np
    import soundfile as sf

    # Wrap raw PCM as WAV in memory so soundfile can decode it
    buf = io.BytesIO()
    import wave
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)           # 16-bit
        wf.setframerate(config.AUDIO_SAMPLE_RATE)
        wf.writeframes(audio_bytes)
    buf.seek(0)

    audio_array, _ = sf.read(buf, dtype="float32")

    segments, _ = model.transcribe(
        audio_array,
        language=language if language != "auto" else None,
        beam_size=5,
        vad_filter=False,            # VAD already handled upstream
        condition_on_previous_text=False,   # stop repetition loops carrying forward
        temperature=0.0,                    # deterministic; no sampling fallback noise
        no_speech_threshold=config.STT_NO_SPEECH_THRESHOLD,
        log_prob_threshold=config.STT_LOGPROB_THRESHOLD,
        compression_ratio_threshold=config.STT_COMPRESSION_THRESHOLD,
    )
    kept = [
        seg.text.strip()
        for seg in segments
        if not _is_hallucinated(
            seg.text,
            getattr(seg, "no_speech_prob", 0.0),
            getattr(seg, "avg_logprob", 0.0),
        )
    ]
    return " ".join(kept).strip()


async def stt_worker(
    worker_id: int,
    stt_queue: asyncio.Queue,
    translate_queue: asyncio.Queue,
    metrics,
):
    """
    Long-running coroutine. Pulls jobs from stt_queue, transcribes,
    pushes to translate_queue.
    """
    model = await asyncio.get_event_loop().run_in_executor(
        _executor, _load_whisper_instance, worker_id
    )

    logger.info(f"STT worker {worker_id} started.")

    while True:
        job: Job = await stt_queue.get()
        job.stt_start = time.monotonic()

        try:
            transcript = await asyncio.get_event_loop().run_in_executor(
                _executor,
                _transcribe,
                model,
                job.audio,
                job.source_lang,
            )
            job.transcript = transcript
            job.stt_end    = time.monotonic()

            if transcript:
                await translate_queue.put(job)
                logger.debug(
                    f"[{job.session_id[:8]}] STT done in "
                    f"{(job.stt_end - job.stt_start)*1000:.0f}ms ({len(transcript)} chars)"
                )
                metrics.record_stt(job)
            else:
                logger.debug(f"[{job.session_id[:8]}] STT returned empty — discarding.")

        except Exception as e:
            logger.error(f"[{job.session_id[:8]}] STT error: {e}", exc_info=True)
            metrics.record_error("stt", job.session_id)
        finally:
            stt_queue.task_done()
