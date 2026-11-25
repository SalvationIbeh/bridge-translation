# Bridge — Real-Time Speech Translation for Support Calls

Bridge translates a live customer-service call in near real time. The customer speaks Spanish and the agent hears English (or the other way round), with the translated audio arriving roughly half a second after the speaker stops.

I built it end to end: a GPU inference server, its container build and CI pipeline, the cloud deployment, load and latency benchmarking, and a native desktop client.

> **Headline result:** about **550 ms end-to-end P50** (speech ends → first translated audio byte, over WebSocket, including network) on a self-hosted GPU stack. That's down from about **2,035 ms P50** with the managed-API prototype. Spanish transcription word error rate is **3.3%** on clean support-call clips.

---

## Architecture

```
 Agent desktop (Tauri + Rust)                GPU server (Docker on RunPod / Modal)
 ┌──────────────────────────┐   WSS + HMAC   ┌──────────────────────────────────────────┐
 │ mic + call audio capture │ ─────────────▶ │ VAD → STT (faster-whisper) → MT → TTS    │
 │ playback of translation  │ ◀───────────── │ per-session async queues, warm models    │
 └──────────────────────────┘   PCM audio    │ /health  /metrics  PII redaction         │
                                             └──────────────────────────────────────────┘
```

- **Server** (`server/`): FastAPI + Uvicorn WebSocket service. Each session runs a staged async pipeline: voice-activity detection → speech-to-text → machine translation → text-to-speech. Models are pre-warmed at boot to remove cold-start latency.
- **Client** (`desktop/`): Tauri/Rust desktop app that captures audio, streams it over WebSocket and plays back the translation. Credentials are stored in the OS keychain.
- **Prototype** (`bridge_spike/`, `live_probe/`): the original managed-API pipeline (Deepgram → OpenAI → ElevenLabs/OpenAI TTS), used as the latency baseline.

## DevOps & infrastructure highlights

| Area | What's in the repo |
|---|---|
| **Containerisation** | CUDA-based `server/Dockerfile` with a `.dockerignore`. |
| **CI/CD** | GitHub Actions workflow (`.github/workflows/build-server.yml`) builds and pushes the server image when `server/**` changes. It frees runner disk space first to fit an ~8 GB CUDA image. |
| **Cloud GPU deployment** | Deployed and benchmarked on RunPod GPU pods and Modal (T4). Includes a written cost and availability comparison of home GPU vs always-warm cloud (`docs/decisions/`). |
| **Observability** | `/health` readiness endpoint (warm-up state, session capacity) and a `/metrics` endpoint (`server/monitoring/`). |
| **Security** | HMAC-signed WebSocket auth tokens with resumption (`server/auth.py`), PII redaction in the pipeline (`server/pipeline/redaction.py`), and a PCI-avoidance design decision. |
| **Reliability** | Liveness checks, per-session queue management, warm-up failure tracking, and a persistence/availability audit script. |
| **Runbooks** | Step-by-step deploy and test runbooks for each stage (`docs/STAGE2_*_RUNBOOK.md`). |
| **Benchmarking** | Scripted latency, load, alternating-direction and multi-process concurrency benchmarks (`scripts/`). Raw results are in `runs/`. |
| **Decision records** | 13 architecture decision records covering model choice, wire format, auth, cloud vs self-hosted, and more (`docs/decisions/`). |

## Results

| Measurement | Result | Source |
|---|---|---|
| Managed-API prototype, post-speech P50 | ~2,035 ms | `docs/STAGE1_LATENCY_REPORT.md` |
| Self-hosted GPU, end-to-end P50 (incl. network) | **~550 ms** | `docs/STAGE2_DESIGN.md` |
| Warm inference only, P50 (Modal T4) | **413 ms** | `docs/STAGE2_DESIGN.md` |
| Spanish STT word error rate (Whisper small) | **3.3%** | `docs/STAGE2_DESIGN.md` |

## Tech stack

**Languages:** Python, Rust, JavaScript
**Server:** FastAPI, Uvicorn, WebSockets, faster-whisper, Hugging Face Transformers, Piper TTS
**Infrastructure:** Docker, GitHub Actions, RunPod GPU, Modal
**Client:** Tauri, Rust

## Running it

**Prototype in mock mode** (no API keys needed):

```bash
python scripts/run_stage1.py --mode mock
```

**Server** (needs an NVIDIA GPU). Full steps are in `docs/STAGE2_S1_RUNBOOK.md`:

```bash
docker build -t bridge-server server/
docker run --gpus all -p 8000:8000 -e AUTH_TOKEN=<your-test-token> bridge-server
curl http://localhost:8000/health
```

**Benchmark against a running server:**

```bash
SERVER_URL=ws://localhost:8000/ws/translate AUTH_TOKEN=<your-test-token> \
  python scripts/benchmark_s1_telephony.py --runs 3
```

The product spec is in `docs/PRD.md`, the technical design in `docs/TRD.md` and `docs/STAGE2_DESIGN.md`, and the roadmap in `docs/ROADMAP.md`.

## Tests

```bash
cd server && python -m pytest
```

These cover liveness, PII redaction and STT hallucination filtering.

---

*Built by Salvation Ibeh. All audio samples are synthetic; no real customer audio is stored here.*
