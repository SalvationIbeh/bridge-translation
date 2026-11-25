"""
LinguaBridge Server — main.py
FastAPI app with WebSocket endpoint for real-time translation.
Handles 20 concurrent sessions via shared inference worker pools.
"""
import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

import auth
import config
from monitoring.metrics         import Metrics
from pipeline.queue_manager     import QueueManager
from pipeline.vad_worker        import VADWorker, _load_vad
from pipeline.translate_worker  import model_cache
from pipeline.warmup            import run_warmup
from pipeline.liveness          import run_liveness_loop, try_deliver_probe
from pipeline.redaction         import RedactionGate
from pipeline.job               import Job

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    stream=sys.stdout,
    level=getattr(logging, config.LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("linguabridge")

# ── Global singletons ─────────────────────────────────────────────────────────
metrics = Metrics()
queue_mgr: Optional[QueueManager] = None

# session_id → WebSocket — used by result router to deliver audio back
_sessions: dict[str, WebSocket] = {}
_sessions_lock = asyncio.Lock()

# session_id → Future — synthetic liveness probes (S7). result_router delivers a
# probe's completed Job to its future instead of a WebSocket.
_probes: dict[str, asyncio.Future] = {}
_liveness_task: Optional[asyncio.Task] = None

# Single-use nonce tracking for signed tokens (in-memory, single-pod — see auth.py)
_nonce_cache = auth.NonceCache()


def _check_ws_auth(presented: str) -> tuple[bool, str]:
    """Signed-token mode when AUTH_SIGNING_KEY is set; legacy static otherwise."""
    if config.AUTH_SIGNING_KEY:
        return auth.validate_token(presented, config.AUTH_SIGNING_KEY,
                                   config.TOKEN_TTL_S, _nonce_cache)
    if config.AUTH_TOKEN and presented != config.AUTH_TOKEN:
        return False, "invalid static token"
    return True, "ok"


# ── Lifespan: load models, start workers ─────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global queue_mgr
    logger.info("=== LinguaBridge Server starting ===")

    # Prod tripwire: never boot the legacy static-token path in production. The
    # insecure mode is structurally absent in prod, not merely discouraged
    # (docs/decisions/2026-06-20-ws-auth-resumption-token.md, control #4).
    if auth.prod_static_mode_forbidden(config.ENV, config.AUTH_SIGNING_KEY):
        raise RuntimeError(
            "Refusing to boot: legacy static-token auth is forbidden when ENV=prod. "
            "Set AUTH_SIGNING_KEY to enable signed-token mode "
            "(docs/decisions/2026-06-20-ws-auth-resumption-token.md)."
        )

    if config.AUTH_SIGNING_KEY:
        logger.info("Auth: signed-token mode (HMAC, TTL<=%ss).", config.TOKEN_TTL_S)
    else:
        logger.warning("Auth: LEGACY STATIC TOKEN MODE — set AUTH_SIGNING_KEY for "
                       "signed short-lived tokens. Do not run this mode on a public "
                       "endpoint (S3, docs/decisions/2026-06-11).")

    # Pre-load Silero VAD (shared across all sessions)
    t = time.perf_counter()
    _load_vad()
    metrics.record_model_load("vad", (time.perf_counter() - t) * 1000)

    # Start inference worker pools
    queue_mgr = QueueManager(metrics)
    await queue_mgr.start()

    # Pre-load translation models so the first live request doesn't pay the ~4s
    # Opus-MT load (STAGE2_DESIGN §6: pre-load language pairs at boot, off the hot path).
    loop = asyncio.get_running_loop()
    for pair in (p.strip() for p in config.PRELOAD_PAIRS.split(",") if p.strip()):
        try:
            src, tgt = pair.split("-")
            t = time.perf_counter()
            await model_cache.load(src, tgt, loop)
            metrics.record_model_load(f"mt_{src}_{tgt}", (time.perf_counter() - t) * 1000)
            logger.info(f"Pre-loaded MT model: {src}->{tgt}")
        except Exception as e:
            logger.warning(f"MT preload failed for '{pair}': {e}")

    # Boot self-test / pre-warm (§4.4). MUST run before result_router starts —
    # it drains result_queue directly to capture its own warm-up jobs. Flips
    # metrics.ready on success; on failure the node stays not-ready (/health 503).
    await run_warmup(queue_mgr, metrics)

    # Start result router (sends completed jobs back to session WebSockets)
    asyncio.create_task(result_router(), name="result-router")

    # Start periodic deep-liveness re-check (S7) — only once warm, so it never
    # races boot warm-up. A dead GPU between calls now flips ready=0 within
    # ~LIVENESS_INTERVAL_S instead of silently staying "ready".
    global _liveness_task
    if config.LIVENESS_CHECK_ENABLED and metrics.ready:
        _liveness_task = asyncio.create_task(
            run_liveness_loop(metrics, _probe_pipeline_once,
                              config.LIVENESS_INTERVAL_S, config.LIVENESS_TIMEOUT_S,
                              config.LIVENESS_FAILURE_THRESHOLD),
            name="liveness")
    elif config.LIVENESS_CHECK_ENABLED:
        logger.warning("Liveness loop NOT started — node never became warm at boot.")

    logger.info("=== Server ready ===" if metrics.ready else "=== Server up but NOT warm — /health will report 503 ===")
    yield

    # Shutdown
    if _liveness_task is not None:
        _liveness_task.cancel()
    logger.info("Shutting down worker pools...")
    await queue_mgr.stop()


app = FastAPI(title="LinguaBridge Translation Server", lifespan=lifespan)


# ── Liveness probe (S7) ───────────────────────────────────────────────────────
async def _probe_pipeline_once(timeout_s: float) -> tuple[bool, Optional[float]]:
    """Fire ONE MT+TTS job through the live queues and wait for its result.
    Returns (ok, elapsed_ms). `ok` = the probe produced TTS audio within timeout —
    proof the MT and TTS GPU models still execute. Injected into run_liveness_loop."""
    import uuid

    src, tgt = _first_preload_pair()
    sid = f"__liveness__{uuid.uuid4().hex[:8]}"
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _probes[sid] = fut
    t0 = time.perf_counter()
    try:
        await queue_mgr.translate_queue.put(Job(
            session_id=sid, source_lang=src, target_lang=tgt,
            transcript="hola, esto es una prueba" if src == "es" else "hello, this is a test",
        ))
        job = await asyncio.wait_for(fut, timeout=timeout_s)
    except asyncio.TimeoutError:
        return False, None
    finally:
        _probes.pop(sid, None)
    return bool(getattr(job, "tts_audio", None)), (time.perf_counter() - t0) * 1000


def _first_preload_pair() -> tuple[str, str]:
    for pair in (p.strip() for p in config.PRELOAD_PAIRS.split(",") if p.strip()):
        if "-" in pair:
            src, tgt = pair.split("-", 1)
            return src, tgt
    return "es", "en"


# ── Result Router ─────────────────────────────────────────────────────────────
async def result_router():
    """
    Drains result_queue and delivers completed jobs to the correct
    WebSocket session. Runs as a background task.
    """
    while True:
        job = await queue_mgr.result_queue.get()

        # Liveness probe results go to their waiting future, not a WebSocket.
        if try_deliver_probe(_probes, job):
            queue_mgr.result_queue.task_done()
            continue

        try:
            async with _sessions_lock:
                ws = _sessions.get(job.session_id)

            if ws is None:
                logger.debug(f"[{job.session_id[:8]}] Session gone — dropping result.")
                continue

            # Send transcript JSON first, then binary audio
            def _ms(start, end):
                return round((end - start) * 1000) if start and end else None

            transcript_frame = json.dumps({
                "type":         "transcript",
                "original":     job.transcript,
                "translated":   job.translated,
                "latency_ms":   round(job.total_ms or 0),
                "source_lang":  job.source_lang,
                "target_lang":  job.target_lang,
                "stage_ms": {
                    "stt":       _ms(job.stt_start, job.stt_end),
                    "translate": _ms(job.translate_start, job.translate_end),
                    "tts":       _ms(job.tts_start, job.tts_end),
                    "queue_wait": round((job.stt_start - job.enqueued_at) * 1000) if job.stt_start else None,
                },
            })
            await ws.send_text(transcript_frame)
            await ws.send_bytes(job.tts_audio)

        except Exception as e:
            logger.warning(f"[{job.session_id[:8]}] Result delivery failed: {e}")
        finally:
            queue_mgr.result_queue.task_done()


# ── WebSocket Endpoint ─────────────────────────────────────────────────────────
@app.websocket("/ws/translate")
async def websocket_translate(ws: WebSocket):
    await ws.accept()
    session_id:   Optional[str] = None
    vad_worker:   Optional[VADWorker] = None
    source_lang:  str = "en"
    target_lang:  str = "es"

    try:
        # ── 0. Readiness gate ───────────────────────────────────────────────
        # Refuse live calls until the boot self-test has warmed the GPU path —
        # routing a call to a cold/half-loaded node means a dropped first turn.
        if not metrics.ready:
            await ws.send_text(json.dumps({
                "type":    "error",
                "code":    "NOT_READY",
                "message": "Server is warming up. Retry shortly.",
            }))
            await ws.close()
            return

        # ── 1. Capacity check ───────────────────────────────────────────────
        async with _sessions_lock:
            if len(_sessions) >= config.MAX_SESSIONS:
                await ws.send_text(json.dumps({
                    "type":    "error",
                    "code":    "MAX_SESSIONS",
                    "message": f"Server at capacity ({config.MAX_SESSIONS} sessions). Try again shortly.",
                }))
                await ws.close()
                return

        # ── 2. Init handshake ────────────────────────────────────────────────
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
        init = json.loads(raw)

        if init.get("type") != "init":
            await _send_error(ws, "PROTOCOL_ERROR", "Expected init frame first.")
            return

        # Auth — signed short-lived token (S3) or legacy static fallback.
        # Reason is logged, not sent: don't give an attacker a validation oracle.
        ok, reason = _check_ws_auth(init.get("auth", ""))
        if not ok:
            logger.warning(f"Auth rejected: {reason}")
            await _send_error(ws, "AUTH_FAILED", "Invalid authentication token.")
            return

        import uuid
        session_id  = str(uuid.uuid4())
        source_lang = init.get("source_lang", "en")
        target_lang = init.get("target_lang", "es")

        # Register session
        async with _sessions_lock:
            _sessions[session_id] = ws
        metrics.session_opened()

        # Pin translation model for this language pair (prevent LRU eviction)
        model_cache.pin(source_lang, target_lang)

        # Create VAD worker for this session
        async def on_utterance_ready(job):
            await queue_mgr.stt_queue.put(job)

        vad_worker = VADWorker(
            session_id=session_id,
            source_lang=source_lang,
            target_lang=target_lang,
            on_utterance=on_utterance_ready,
        )

        ready_frame = {
            "type":       "ready",
            "session_id": session_id,
            "queue_depth": 0,
        }
        # Option C (ws-auth resumption token): hand the client a fresh single-use HMAC
        # token so it can re-auth on reconnect without a standing secret. Minted on every
        # `ready` → rotates each (re)connection; TTL <= TOKEN_TTL_S bounds the blast radius.
        # Only meaningful in signed-token mode — with no signing key there's nothing to mint
        # (and ENV=prod already refuses to boot in that state).
        # docs/decisions/2026-06-20-ws-auth-resumption-token.md
        if config.AUTH_SIGNING_KEY:
            ready_frame["resumption_token"] = auth.mint_token(
                config.AUTH_SIGNING_KEY, config.TOKEN_TTL_S)
        await ws.send_text(json.dumps(ready_frame))

        logger.info(f"Session opened: {session_id[:8]} | {source_lang}→{target_lang}")

        # ── 3. Main audio receive loop ───────────────────────────────────────
        last_activity = time.monotonic()
        # Per-connection rate limit (§5.2): fixed 1s window message counter.
        rate_window_start = time.monotonic()
        rate_window_count = 0
        # Redaction mode (§8.4 PCI-avoidance — docs/decisions/2026-06-14): while
        # active, inbound audio is DROPPED before VAD/STT so a flagged card-entry
        # segment is never transcribed, translated, synthesized, or logged. The
        # client should also drop frames at source; this is the server enforcement.
        redaction = RedactionGate()

        while True:
            # Session idle timeout
            if time.monotonic() - last_activity > config.SESSION_TIMEOUT_S:
                await _send_error(ws, "SESSION_TIMEOUT", "Session timed out due to inactivity.")
                break

            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
            except asyncio.TimeoutError:
                # Send queue status ping to keep connection alive
                depths = queue_mgr.queue_depths()
                await ws.send_text(json.dumps({
                    "type":             "queue_status",
                    "stt_depth":        depths["stt"],
                    "translate_depth":  depths["translate"],
                    "tts_depth":        depths["tts"],
                }))
                continue

            # Abuse limits (§5.2) — applied to every inbound frame, text or binary.
            now_mono = time.monotonic()
            if now_mono - rate_window_start >= 1.0:
                rate_window_start, rate_window_count = now_mono, 0
            rate_window_count += 1
            if rate_window_count > config.MAX_MSGS_PER_SEC:
                await _send_error(ws, "RATE_LIMITED",
                                  f"Exceeded {config.MAX_MSGS_PER_SEC} messages/sec.")
                break
            frame_len = len(msg.get("bytes") or msg.get("text") or "")
            if frame_len > config.MAX_FRAME_BYTES:
                await _send_error(ws, "FRAME_TOO_LARGE",
                                  f"Frame exceeds {config.MAX_FRAME_BYTES} bytes.")
                break

            if "bytes" in msg:
                last_activity = time.monotonic()
                # Redaction mode: drop the audio frame entirely — it never reaches
                # VAD/STT/MT/TTS/logs. Bridge stays out of the card-data path (§8.4).
                if redaction.should_drop_audio():
                    continue
                # Audio chunk — feed to VAD
                pcm_chunk = msg["bytes"]
                utterance_ready = vad_worker.feed(pcm_chunk)
                if utterance_ready:
                    job = vad_worker.flush()
                    await queue_mgr.stt_queue.put(job)

            elif "text" in msg:
                # Control frame
                frame = json.loads(msg["text"])
                ftype = frame.get("type")
                if ftype == "redact_start":
                    # Enter redaction: drop any audio already buffered (it may hold
                    # the lead-in to the card number) and gate further frames.
                    redaction.start()
                    if vad_worker:
                        vad_worker.discard()
                    await ws.send_text(json.dumps({"type": "redaction", "active": True}))
                    logger.info(f"[{session_id[:8]}] Redaction mode ON — audio dropped pre-STT.")
                elif ftype == "redact_end":
                    redaction.end()
                    if vad_worker:
                        vad_worker.discard()   # clean slate; no carry-over into clear audio
                    await ws.send_text(json.dumps({"type": "redaction", "active": False}))
                    logger.info(f"[{session_id[:8]}] Redaction mode OFF.")
                elif ftype == "end_of_stream":
                    # Force-flush whatever the VAD has buffered — even if VAD
                    # didn't mark speech, the STT worker will handle silence gracefully.
                    # Suppressed while redacting (nothing should be buffered anyway).
                    if vad_worker and vad_worker.has_audio and not redaction.active:
                        job = vad_worker.flush()
                        await queue_mgr.stt_queue.put(job)
                        logger.debug(f"[{session_id[:8]}] end_of_stream: flushed VAD buffer")
                elif ftype == "ping":
                    await ws.send_text(json.dumps({
                        "type":      "pong",
                        "ts":        frame.get("ts"),
                        "server_ts": time.time(),
                    }))

    except WebSocketDisconnect:
        logger.info(f"Session disconnected: {(session_id or 'pre-init')[:8]}")
    except asyncio.TimeoutError:
        logger.warning(f"Session init timeout: {(session_id or 'unknown')[:8]}")
    except Exception as e:
        logger.error(f"Session error [{(session_id or 'unknown')[:8]}]: {e}", exc_info=True)
    finally:
        if session_id:
            async with _sessions_lock:
                _sessions.pop(session_id, None)
            model_cache.unpin(source_lang, target_lang)
            metrics.session_closed()
            logger.info(f"Session cleaned up: {session_id[:8]}")


# ── HTTP Endpoints ────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"status": "ok"}


@app.get("/health")
async def health(response: Response):
    """Readiness probe. Returns 503 until the boot self-test has warmed the GPU
    path (§4.4), so a Docker HEALTHCHECK / orchestrator restarts a node that
    never becomes warm. `warm` carries the warm-success / cold-start artifact."""
    warm = metrics.snapshot()["warm"]
    if not warm["ready"]:
        response.status_code = 503
    return {
        "status":          "ok" if warm["ready"] else "warming_up",
        "ready":           warm["ready"],
        "warm":            warm,
        "active_sessions": metrics.active_sessions,
        "max_sessions":    config.MAX_SESSIONS,
        "queue_depths":    queue_mgr.queue_depths() if queue_mgr else {},
    }


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    return metrics.prometheus()


# ── Helpers ───────────────────────────────────────────────────────────────────
async def _send_error(ws: WebSocket, code: str, message: str):
    try:
        await ws.send_text(json.dumps({"type": "error", "code": code, "message": message}))
    except Exception:
        pass


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        log_level=config.LOG_LEVEL.lower(),
        ws_ping_interval=20,
        ws_ping_timeout=10,
    )
