"""
LinguaBridge Server Config
All tunables in one place — override via environment variables.
"""
import os

# ── Capacity ────────────────────────────────────────────────────────────────
MAX_SESSIONS        = int(os.getenv("MAX_SESSIONS", "20"))

# ── Worker pool sizes ────────────────────────────────────────────────────────
STT_WORKERS         = int(os.getenv("STT_WORKERS", "3"))
TRANSLATE_WORKERS   = int(os.getenv("TRANSLATE_WORKERS", "3"))
TTS_WORKERS         = int(os.getenv("TTS_WORKERS", "3"))

# ── CUDA device assignment ───────────────────────────────────────────────────
STT_DEVICE          = os.getenv("STT_DEVICE", "cpu")
TRANSLATE_DEVICE    = os.getenv("TRANSLATE_DEVICE", "cpu")
TTS_DEVICE          = os.getenv("TTS_DEVICE", "cpu")

# ── Model selection ──────────────────────────────────────────────────────────
WHISPER_MODEL       = os.getenv("WHISPER_MODEL", "base")          # tiny/base/small/medium
# int8 works on CPU; use float16 on GPU
WHISPER_COMPUTE     = os.getenv("WHISPER_COMPUTE", "int8")
DEFAULT_TTS_VOICE   = os.getenv("DEFAULT_TTS_VOICE", "en_US-lessac-medium")
MODELS_DIR          = os.getenv("MODELS_DIR", os.path.join(os.path.dirname(__file__), "..", "models"))

# Translation pairs to pre-load at boot (comma-separated src-tgt) so the first live
# request doesn't pay the ~4s Opus-MT load (STAGE2_DESIGN §6). Empty = load on demand.
PRELOAD_PAIRS       = os.getenv("PRELOAD_PAIRS", "es-en,en-es")

# ── STT anti-hallucination ───────────────────────────────────────────────────
# Whisper was trained on scraped subtitles and INVENTS text on near-silence /
# ambient noise ("Subscribe!", "Subtitles by Amara.org", repetition loops). These
# gate it: segments with high no-speech prob AND low confidence are dropped, and a
# marker denylist catches the known artifacts. Found in the 2026-06-19 live probe
# (silent mic produced a full fake conversation).
STT_NO_SPEECH_THRESHOLD   = float(os.getenv("STT_NO_SPEECH_THRESHOLD", "0.6"))
STT_LOGPROB_THRESHOLD     = float(os.getenv("STT_LOGPROB_THRESHOLD", "-1.0"))
STT_COMPRESSION_THRESHOLD = float(os.getenv("STT_COMPRESSION_THRESHOLD", "2.4"))

# ── VAD ──────────────────────────────────────────────────────────────────────
# Defaults tuned 2026-06-20 (STT Council rec #2): raise the speech bar so noise,
# echo tails and silence don't reach Whisper and trigger hallucinations. Threshold
# 0.5→0.6 (fewer false-speech windows on noisy CS audio); min-speech 300→500ms
# (a click/echo blip can't accumulate 500ms of speech-classed windows). SILENCE_MS
# left at 800: it's the wait-for-utterance-end timer — raising it adds turn latency.
VAD_THRESHOLD       = float(os.getenv("VAD_THRESHOLD", "0.6"))
VAD_SILENCE_MS      = int(os.getenv("VAD_SILENCE_MS", "800"))
VAD_MIN_SPEECH_MS   = int(os.getenv("VAD_MIN_SPEECH_MS", "500"))
VAD_MAX_BUFFER_S    = int(os.getenv("VAD_MAX_BUFFER_S", "30"))
AUDIO_SAMPLE_RATE   = 16000
AUDIO_CHUNK_MS      = 20
AUDIO_CHUNK_SAMPLES = AUDIO_SAMPLE_RATE * AUDIO_CHUNK_MS // 1000  # 320 samples

# ── Session ──────────────────────────────────────────────────────────────────
SESSION_TIMEOUT_S   = int(os.getenv("SESSION_TIMEOUT_S", "300"))

# ── Warm-up / readiness (§4.4 reliability path) ───────────────────────────────
# A boot-time self-test fires one inference through every GPU model so the slow
# first-call CUDA kernel compile is paid off the live-call hot path, and the node
# reports a verifiable warm-success signal on /health + /metrics.
WARMUP_ENABLED      = os.getenv("WARMUP_ENABLED", "1").lower() not in ("0", "false", "no")
# The very first GPU inference compiles CUDA kernels and can take tens of seconds;
# keep this generous so a slow-but-healthy boot isn't flagged as a failure.
WARMUP_TIMEOUT_S    = float(os.getenv("WARMUP_TIMEOUT_S", "60"))

# ── Auth (S3 — docs/decisions/2026-06-11-s3-ws-auth-hmac-tokens.md) ──────────
# AUTH_SIGNING_KEY set  → signed-token mode: clients present short-lived HMAC
#   tokens minted by scripts/mint_token.py with the same key. AUTH_TOKEN ignored.
#   Must be a FRESH secret — never reuse the old static AUTH_TOKEN value.
# AUTH_SIGNING_KEY unset → legacy static AUTH_TOKEN mode (local dev only; the
#   server logs a loud warning at boot).
AUTH_SIGNING_KEY    = os.getenv("AUTH_SIGNING_KEY", "")
TOKEN_TTL_S         = int(os.getenv("TOKEN_TTL_S", "300"))   # max token lifetime (≤5 min)
AUTH_TOKEN          = os.getenv("AUTH_TOKEN", "")   # legacy fallback — see above

# Deployment environment. ENV=prod FORBIDS the legacy static-token path: the server
# refuses to boot in static mode (no AUTH_SIGNING_KEY) when prod, so the insecure mode
# is structurally absent rather than policed (docs/decisions/2026-06-20-ws-auth-resumption-token.md).
ENV                 = os.getenv("ENV", "dev")

# ── Endpoint abuse limits (§5.2 hardening deltas) ────────────────────────────
# Real-time audio is 50 × 640-byte frames/sec; both caps are generous multiples.
MAX_FRAME_BYTES     = int(os.getenv("MAX_FRAME_BYTES", str(64 * 1024)))
MAX_MSGS_PER_SEC    = int(os.getenv("MAX_MSGS_PER_SEC", "200"))

# ── Periodic deep-liveness (§4.4 reliability path / S7) ──────────────────────
# The boot self-test flips `ready` True once; without a re-check a silently-dead
# GPU (lost CUDA context, driver crash, OOM) keeps /health green and the
# orchestrator keeps routing calls to a corpse. A periodic probe fires one real
# MT+TTS inference through the live queues; `ready` flips 0 after N consecutive
# failures and back to 1 on recovery.
LIVENESS_CHECK_ENABLED     = os.getenv("LIVENESS_CHECK_ENABLED", "1").lower() not in ("0", "false", "no")
LIVENESS_INTERVAL_S        = float(os.getenv("LIVENESS_INTERVAL_S", "30"))
LIVENESS_TIMEOUT_S         = float(os.getenv("LIVENESS_TIMEOUT_S", "10"))
# Flip not-ready only after this many CONSECUTIVE failures — avoids a single
# under-load timeout flapping the node out of rotation.
LIVENESS_FAILURE_THRESHOLD = int(os.getenv("LIVENESS_FAILURE_THRESHOLD", "2"))

# ── Synthetic sessions (warm-up + liveness probes) ───────────────────────────
# Jobs whose session_id starts with one of these are internal self-tests, not
# real traffic — workers skip latency/throughput metrics for them so the probes
# don't pollute the percentiles or jobs_processed/jobs_failed counters.
SYNTHETIC_SESSION_PREFIXES = ("__warmup", "__liveness")


def is_synthetic(session_id: str) -> bool:
    return bool(session_id) and session_id.startswith(SYNTHETIC_SESSION_PREFIXES)


# ── Logging ──────────────────────────────────────────────────────────────────
LOG_LEVEL           = os.getenv("LOG_LEVEL", "INFO")
