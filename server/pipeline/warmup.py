"""
Boot-time warm-up / self-test (STAGE2_DESIGN section 4.4 reliability path).

Pushes synthetic jobs through the real queue pipeline at boot so that:

1. Every configured GPU model fires at least one inference before the first live call.
2. /health and /metrics expose a verifiable warm-success signal with per-stage timings.

Runs once in the FastAPI lifespan, after workers + MT preload but before the
result_router starts, so it can drain result_queue directly without racing it.

Two job types cover all models without depending on ASR transcribing synthetic audio:

* STT job: synthetic PCM, src == tgt so translation is a passthrough. Whether or
  not the transcript is empty, Whisper has been invoked.
* MT/TTS jobs: one transcript-injected job per configured PRELOAD_PAIRS entry.
  These gate readiness because they guarantee each configured MT direction and
  target-language Piper voice actually run.
"""
import asyncio
import logging
import time

import config
from pipeline.job import Job

logger = logging.getLogger(__name__)

_STT_SESSION = "__warmup_stt__"
_MT_SESSION_PREFIX = "__warmup_mt__"

_WARMUP_PHRASE = {
    "es": "hola, esto es una prueba",
    "en": "hello, this is a test",
    "fr": "bonjour, ceci est un test",
    "de": "hallo, das ist ein test",
    "pt": "ola, isto e um teste",
}


def _synthetic_pcm(seconds: float = 1.0) -> bytes:
    """Return 16kHz 16-bit mono PCM with speech-band energy for STT warm-up."""
    import numpy as np

    sr = config.AUDIO_SAMPLE_RATE
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    wave = (
        0.30 * np.sin(2 * np.pi * 180 * t)
        + 0.20 * np.sin(2 * np.pi * 320 * t)
        + 0.05 * np.random.randn(len(t))
    )
    pcm = np.clip(wave, -1.0, 1.0)
    return (pcm * 32767).astype(np.int16).tobytes()


def _stage_ms(job: Job, start_attr: str, end_attr: str):
    a = getattr(job, start_attr, None)
    b = getattr(job, end_attr, None)
    return (b - a) * 1000 if a and b else None


def _configured_pairs() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for pair in (p.strip() for p in config.PRELOAD_PAIRS.split(",") if p.strip()):
        try:
            src, tgt = pair.split("-")
        except ValueError:
            logger.warning("Malformed PRELOAD_PAIRS entry %r; skipping warm-up for it.", pair)
            continue
        pairs.append((src, tgt))
    if not pairs:
        logger.warning("No valid PRELOAD_PAIRS entries; defaulting warm-up to es-en.")
        pairs = [("es", "en")]
    return pairs


async def run_warmup(queue_mgr, metrics) -> bool:
    """Fire one inference through every configured model and flip readiness on success."""
    if not config.WARMUP_ENABLED:
        logger.warning("Warm-up disabled (WARMUP_ENABLED=0); marking ready WITHOUT a self-test.")
        metrics.mark_ready()
        return True

    t0 = time.perf_counter()
    pairs = _configured_pairs()
    pair_label = ", ".join(f"{src}->{tgt}" for src, tgt in pairs)
    logger.info("Warm-up: firing self-test through STT + MT/TTS pairs [%s]...", pair_label)

    # STT warm-up uses the first target language as passthrough. Its output is not
    # part of readiness because synthetic audio may transcribe to empty text.
    stt_lang = pairs[0][1]
    await queue_mgr.stt_queue.put(Job(
        session_id=_STT_SESSION,
        source_lang=stt_lang,
        target_lang=stt_lang,
        audio=_synthetic_pcm(),
    ))

    # Guaranteed MT + TTS warm-up jobs.
    mt_sessions: dict[str, tuple[str, str]] = {}
    for src, tgt in pairs:
        session_id = f"{_MT_SESSION_PREFIX}{src}_{tgt}"
        mt_sessions[session_id] = (src, tgt)
        await queue_mgr.translate_queue.put(Job(
            session_id=session_id,
            source_lang=src,
            target_lang=tgt,
            transcript=_WARMUP_PHRASE.get(src, "hola, esto es una prueba"),
        ))

    captured: dict[str, Job] = {}
    deadline = time.monotonic() + config.WARMUP_TIMEOUT_S
    while not all(session_id in captured for session_id in mt_sessions):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            job = await asyncio.wait_for(queue_mgr.result_queue.get(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        try:
            captured[job.session_id] = job
        finally:
            queue_mgr.result_queue.task_done()

    # Give the STT passthrough job a brief chance to land for timing only.
    if all(session_id in captured for session_id in mt_sessions):
        grace = min(deadline, time.monotonic() + 3.0)
        while _STT_SESSION not in captured:
            remaining = grace - time.monotonic()
            if remaining <= 0:
                break
            try:
                job = await asyncio.wait_for(queue_mgr.result_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            try:
                captured[job.session_id] = job
            finally:
                queue_mgr.result_queue.task_done()

    stt_job = captured.get(_STT_SESSION)
    mt_jobs = {
        session_id: captured.get(session_id)
        for session_id in mt_sessions
    }

    stage_ms: dict[str, float] = {}
    if stt_job:
        stage_ms["stt"] = _stage_ms(stt_job, "stt_start", "stt_end")
    for session_id, mt_job in mt_jobs.items():
        if not mt_job:
            continue
        src, tgt = mt_sessions[session_id]
        stage_ms[f"translate_{src}_{tgt}"] = _stage_ms(mt_job, "translate_start", "translate_end")
        stage_ms[f"tts_{tgt}"] = _stage_ms(mt_job, "tts_start", "tts_end")
    stage_ms = {k: v for k, v in stage_ms.items() if v is not None}

    success = all(mt_job is not None and bool(mt_job.tts_audio) for mt_job in mt_jobs.values())
    elapsed_ms = (time.perf_counter() - t0) * 1000

    if success:
        warmup_ms = sum(stage_ms.values()) or None
        metrics.mark_ready(warmup_ms=warmup_ms, stage_ms=stage_ms)
        logger.info(
            "Warm-up OK in %.0fms; node READY | pairs=[%s] stages=%s "
            "(stt_job_completed=%s)",
            elapsed_ms,
            pair_label,
            stage_ms,
            stt_job is not None,
        )
    else:
        metrics.mark_warmup_failed()
        missing = [
            f"{src}->{tgt}"
            for session_id, (src, tgt) in mt_sessions.items()
            if not (mt_jobs.get(session_id) is not None and mt_jobs[session_id].tts_audio)
        ]
        logger.error(
            "Warm-up FAILED after %.0fms; node NOT ready "
            "(missing MT/TTS success for %s). /health will report 503.",
            elapsed_ms,
            ", ".join(missing) or "unknown pairs",
        )
    return success
