# LinguaBridge Server - RunPod Dockerfile
# Base: CUDA 12.4 + Python 3.11
FROM runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04

WORKDIR /app

# System deps: audio processing + cuDNN 8 (required by CTranslate2 for GPU inference)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libsndfile1 \
    libcudnn8 \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# piper-tts pulls CPU `onnxruntime` as a dependency, which shadows `onnxruntime-gpu`
# and silently forces Piper onto CPU (~2,800ms TTS vs ~150ms on GPU). Remove the CPU
# build so only onnxruntime-gpu remains and Piper uses CUDAExecutionProvider.
RUN pip uninstall -y onnxruntime && pip install --no-cache-dir --force-reinstall onnxruntime-gpu==1.26.0

# Download Silero VAD (cached in image)
# trust_repo=True is required in CI/non-interactive builds; without it torch.hub
# blocks on a trust prompt and the build hangs/fails (matches vad_worker._load_vad).
RUN python -c "import torch; torch.hub.load('snakers4/silero-vad', 'silero_vad', force_reload=True, trust_repo=True)"

# Bake and runtime MUST read the same caches, or boot re-downloads everything:
# - Opus-MT: translate_worker loads with cache_dir=MODELS_DIR, so bake there too.
# - Whisper: stt_worker passes no download_root, so bake into the default HF cache.
ENV MODELS_DIR=/models
ENV WHISPER_MODEL=small

# Pre-download Opus-MT es<->en into MODELS_DIR (baked into image, no runtime fetch).
RUN python -c "\
import os; \
from transformers import MarianMTModel, MarianTokenizer; \
d = os.environ['MODELS_DIR']; \
MarianTokenizer.from_pretrained('Helsinki-NLP/opus-mt-es-en', cache_dir=d); \
MarianMTModel.from_pretrained('Helsinki-NLP/opus-mt-es-en', cache_dir=d); \
MarianTokenizer.from_pretrained('Helsinki-NLP/opus-mt-en-es', cache_dir=d); \
MarianMTModel.from_pretrained('Helsinki-NLP/opus-mt-en-es', cache_dir=d)"

# Pre-download faster-whisper weights (CTranslate2 format) into the default HF cache.
RUN python -c "\
import os; \
from faster_whisper import download_model; \
download_model(os.environ['WHISPER_MODEL'])"

# Pre-download Piper TTS voices: en_US lessac medium + es_ES mls_10246 low.
RUN mkdir -p /models/tts && \
    python -c "\
import urllib.request; \
voices = [ \
    ('https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium', 'en_US-lessac-medium'), \
    ('https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_ES/mls_10246/low', 'es_ES-mls_10246-low'), \
]; \
[ \
    (urllib.request.urlretrieve(base + '/' + name + '.onnx', '/models/tts/' + name + '.onnx'), \
     urllib.request.urlretrieve(base + '/' + name + '.onnx.json', '/models/tts/' + name + '.onnx.json')) \
    for base, name in voices \
]"

# Copy server code
COPY . .

# Expose WebSocket + metrics port
EXPOSE 8000

# GPU defaults: single-GPU pod (all workers share cuda:0)
# Override in RunPod env vars if using multi-GPU.
# MAX_SESSIONS=4: measured S2 ceiling (2026-06-13). Server-side P95 < 2000ms holds
# to ~4 concurrent sessions; beyond that per-turn latency tails over the gate. This is
# a PER-PROCESS limit — not GPU-bound (A6000 idled at 33% util, RTX 4000 Ada at 83%) and
# not worker-bound (3/3/3 killed queue_wait but not P95). Scale out with more pods, not a
# bigger GPU. Runs: runs/s2-load-2026061{3}-*.json. Workers stay 2/2/2 (3/3/3 gave no gain).
ENV MAX_SESSIONS=4
ENV STT_WORKERS=2
ENV TRANSLATE_WORKERS=2
ENV TTS_WORKERS=2
ENV STT_DEVICE=cuda:0
ENV TRANSLATE_DEVICE=cuda:0
ENV TTS_DEVICE=cuda:0
ENV WHISPER_COMPUTE=float16
ENV PRELOAD_PAIRS=es-en,en-es
ENV LOG_LEVEL=INFO
# AUTH_SIGNING_KEY must be set in the RunPod pod environment (no default) — a fresh
# secret, never the old AUTH_TOKEN value. Clients connect with short-lived tokens
# minted by scripts/mint_token.py. Without it the server falls back to legacy
# static AUTH_TOKEN mode and logs a loud warning (local dev only).

# Readiness probe. /health returns 200 only after the boot self-test warms the GPU
# path; 503 makes urlopen raise and marks the container unhealthy while warming or
# after warm-up failure. start-period covers cold model-load + first-inference compile.
HEALTHCHECK --interval=30s --timeout=5s --start-period=150s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=4)"

CMD ["python", "main.py"]
