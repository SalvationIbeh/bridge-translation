# Stage 2 — S1 Validation Runbook

**Goal:** prove the self-hosted GPU stack's clean-audio numbers (~550ms P50 / WER 3.3%)
survive **real telephony** — the Skeptic's binding gate (STAGE2_DESIGN §9 S1). Run it once,
end to end, on the RunPod persistent T4.

**Gate to pass:** tier `mulaw8k` → **post-speech P95 < 2000ms AND WER ≤ 8%**.

Direction is **es→en only** (image + samples provision that leg; en→es is a separate
bidirectional-milestone task).

---

## One-time: stand up the pod

1. **Build + push the image** from `server/Dockerfile` (already correct: Whisper small/float16,
   `*_DEVICE=cuda:0`, es→en + `en` voice baked in).
2. **Deploy a RunPod pod**, GPU = **T4**, persistent (not serverless). Expose HTTP port **8000**.
3. Set the pod env: **`AUTH_TOKEN=<a-real-secret>`** (no default — server refuses without it).
4. Note the pod's public URL: `https://<podid>-8000.proxy.runpod.net`
   → WebSocket = `wss://<podid>-8000.proxy.runpod.net/ws/translate`.

## Run everything on the pod (one sitting)

The clean source clips and the generated ladder are gitignored, so copy the clips up first:

```bash
# from your machine — get the clean clips onto the pod
scp -r samples/realtime-pcm  <pod>:/app/samples/
```

```bash
# on the pod (/app), project deps already installed (librosa/soundfile/numpy)
python scripts/make_telephony_ladder.py            # → samples/telephony-pcm/ (μ-law 8kHz + noise)

AUTH_TOKEN=$AUTH_TOKEN MODELS_DIR=/models python server/main.py &   # start server, wait for "Server ready"

SERVER_URL=ws://localhost:8000/ws/translate AUTH_TOKEN=$AUTH_TOKEN \
    python scripts/benchmark_s1_telephony.py --runs 3
```

(Or run the harness **from your machine** against the `wss://…proxy.runpod.net` URL — it needs
only `websockets`, no GPU/audio libs. The ladder generator still needs the project env.)

## Read the result

- Console prints a per-tier table + **`✅ S1 GATE PASS`** / **`❌ S1 GATE FAIL`**.
- Full record: `runs/s1-telephony-<timestamp>.json` (commit it — a benchmark JSON is progress).
- Headline metric: `post_speech_ms` = end_of_stream → translated audio (turn-based, CLAUDE.md §3).
  Cross-check `server_latency_ms` (pipeline only) vs `network_overhead_ms` (WS/RPC).

## Decide

| Outcome | Next |
|---|---|
| **PASS** (mulaw8k P95 < 2000ms, WER ≤ 8%) | Self-host validated. Proceed to **S2** (4-stream load test → real `MAX_SESSIONS`) and **S3** (token-broker + TLS). |
| **FAIL on latency** | Profile stages in the JSON — TTS is the usual lever (stream/chunk Piper); check network_overhead vs server_latency. |
| **FAIL on WER** | μ-law degradation hit STT. Try `WHISPER_MODEL=medium` (re-check latency), or tune VAD endpointing. This is the Skeptic's dissent made real — weigh self-host vs cloud-STT fallback before committing further. |

**Cost:** T4 ≈ $0.50/hr; a 3-run sweep across 3 tiers is a few minutes. Keep within the $300 Stage-1 cap (`runs/cost-ledger.json`).
