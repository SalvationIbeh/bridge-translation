"""
TTS Workers — Piper TTS pool draining the TTS queue.
Synthesizes translated text → raw PCM audio bytes.
Completed jobs go to the result router.
"""
import asyncio
import io
import logging
import os
import time
import wave
from concurrent.futures import ThreadPoolExecutor

import config
from pipeline.job import Job

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=config.TTS_WORKERS, thread_name_prefix="tts")

# Voice mapping: language code → Piper voice model name
# Add voices as needed — download model files to MODELS_DIR
TTS_VOICES = {
    "en":  "en_US-lessac-medium",
    "es":  "es_ES-mls_10246-low",
    "fr":  "fr_FR-mls_1840-low",
    "de":  "de_DE-thorsten-medium",
    "pt":  "pt_BR-faber-medium",
    "zh":  "zh_CN-huayan-medium",
    "ar":  "ar_JO-kareem-medium",
    "ja":  "ja_JP-kokoro-medium",
}


def _load_piper_instance(worker_id: int, voices_needed: list[str]):
    """Load Piper voice models for all needed languages.

    PiperVoice.load(use_cuda=True) explicitly sets CUDAExecutionProvider in the
    ONNX session. Setting TTS_DEVICE env var alone is a no-op — this is the fix.
    """
    from piper import PiperVoice

    use_cuda = "cuda" in config.TTS_DEVICE.lower()
    logger.info(f"TTS worker {worker_id}: loading with use_cuda={use_cuda} (TTS_DEVICE={config.TTS_DEVICE})")

    voices = {}
    for lang in voices_needed:
        voice_name = TTS_VOICES.get(lang)
        if not voice_name:
            logger.warning(f"No TTS voice configured for language: {lang}")
            continue
        model_path = os.path.join(config.MODELS_DIR, "tts", f"{voice_name}.onnx")
        if not os.path.exists(model_path):
            logger.warning(f"TTS model not found: {model_path} — skipping {lang}")
            continue
        voices[lang] = PiperVoice.load(model_path, use_cuda=use_cuda)
        logger.info(f"TTS worker {worker_id}: loaded voice {voice_name} for {lang} (cuda={use_cuda})")
    return voices


def _synthesize(voices: dict, text: str, target_lang: str) -> bytes:
    """
    Blocking synthesis — runs in ThreadPoolExecutor.
    Returns synthesized audio as a WAV bytes object.
    piper-tts 1.2.x API: synthesize(text, wav_file) writes directly to a wave.Wave_write.
    """
    voice = voices.get(target_lang)
    if voice is None:
        raise ValueError(f"No TTS voice loaded for language: {target_lang}")

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit PCM
        wf.setframerate(voice.config.sample_rate)
        voice.synthesize(text, wf)

    audio = buf.getvalue()
    if len(audio) <= 44:  # WAV header only — no audio produced
        raise ValueError(f"Piper returned empty audio for: {text!r}")
    return audio


async def tts_worker(
    worker_id: int,
    tts_queue: asyncio.Queue,
    result_queue: asyncio.Queue,
    metrics,
):
    """
    Long-running coroutine. Pulls jobs from tts_queue, synthesizes audio,
    pushes to result_queue.
    """
    loop = asyncio.get_event_loop()

    # Load voices for all configured languages at startup
    voices_needed = list(TTS_VOICES.keys())
    voices = await loop.run_in_executor(
        _executor, _load_piper_instance, worker_id, voices_needed
    )

    logger.info(f"TTS worker {worker_id} started with voices: {list(voices.keys())}")

    while True:
        job: Job = await tts_queue.get()
        job.tts_start = time.monotonic()

        try:
            audio_bytes = await loop.run_in_executor(
                _executor,
                _synthesize,
                voices,
                job.translated,
                job.target_lang,
            )
            job.tts_audio = audio_bytes
            job.tts_end   = time.monotonic()

            await result_queue.put(job)
            logger.debug(
                f"[{job.session_id[:8]}] TTS done in "
                f"{(job.tts_end - job.tts_start)*1000:.0f}ms | "
                f"total pipeline: {job.total_ms:.0f}ms"
            )
            metrics.record_tts(job)

        except Exception as e:
            logger.error(f"[{job.session_id[:8]}] TTS error: {e}", exc_info=True)
            metrics.record_error("tts", job.session_id)
        finally:
            tts_queue.task_done()
