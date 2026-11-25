"""
Periodic deep-liveness re-check (STAGE2_DESIGN §4.4 reliability path, S7).

The boot self-test (`warmup.py`) flips `metrics.ready` True exactly once. Nothing
re-checks it afterwards, so a GPU that dies *between* calls — lost CUDA context,
driver crash, silent OOM — keeps `/health` returning 200 and the orchestrator
keeps routing live calls to a node that can no longer translate.

This module closes that gap: every `LIVENESS_INTERVAL_S` it fires ONE real MT+TTS
inference through the live queue pipeline (the same reliable path `warmup` gates
on — a transcript-injected job, so it doesn't depend on ASR transcribing synthetic
audio). On `LIVENESS_FAILURE_THRESHOLD` consecutive failures it flips `ready` False;
a single later success restores it. The probe job uses a `__liveness__` session id
so workers skip its metrics (see `config.is_synthetic`).

The loop is pure scheduling/threshold logic. The actual "enqueue a job and capture
its result" dance lives in `main.py` (it needs the result-router's probe registry)
and is injected as the `probe_once` callable, keeping this module unit-testable
without a running server. `try_deliver_probe` is the pure delivery helper the
result-router calls.
"""
import asyncio
import logging

logger = logging.getLogger(__name__)


def try_deliver_probe(probes: dict, job) -> bool:
    """If `job` belongs to a registered liveness probe, hand it to the waiting
    future and report handled=True (the result-router must then NOT try to deliver
    it to a WebSocket). Pure + synchronous so it's trivially testable."""
    fut = probes.get(job.session_id)
    if fut is None:
        return False
    if not fut.done():
        fut.set_result(job)
    return True


async def run_liveness_loop(metrics, probe_once, interval_s: float,
                            timeout_s: float, threshold: int) -> None:
    """Fire a deep-liveness probe every interval_s and update readiness.

    probe_once: async (timeout_s) -> (ok: bool, elapsed_ms: float | None)
    Runs until cancelled (FastAPI lifespan shutdown)."""
    logger.info("Liveness loop started: every %.0fs, timeout %.0fs, "
                "flip-not-ready after %d consecutive failures.",
                interval_s, timeout_s, threshold)
    try:
        while True:
            await asyncio.sleep(interval_s)
            try:
                ok, ms = await probe_once(timeout_s)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — a probe must never kill the loop
                ok, ms = False, None
                logger.error("Liveness probe raised: %s", e)

            was_ready = metrics.ready
            now_ready = metrics.record_liveness(ok, ms, threshold)

            if ok and not was_ready and now_ready:
                logger.warning("Liveness RECOVERED — node READY again (%.0fms probe).", ms or 0)
            elif not ok and was_ready and not now_ready:
                logger.error("Liveness FAILED %d× consecutively — node NOT ready; "
                             "/health now 503 so the orchestrator can restart it.",
                             metrics.consecutive_liveness_failures)
            elif not ok:
                logger.warning("Liveness probe failed (%d consecutive, threshold %d); ready=%s.",
                               metrics.consecutive_liveness_failures, threshold, now_ready)
    except asyncio.CancelledError:
        logger.info("Liveness loop stopped.")
        raise
