# Stage 2 — Bidirectional Validation Runbook (one pod sitting)

**Goal:** validate the en→es reverse leg + alternating directions in a single RunPod
session, per the council conditions (`docs/decisions/2026-06-09-bidirectional-en-es-provisioning.md`):
reverse-leg warm/cold latency, no es→en regression, alternating-direction latency,
GPU memory impact, Spanish output quality.

**Image:** `ghcr.io/salvationibeh/bridge-server:latest` built from `d97ff0f`
(both MT directions, both Piper voices, `PRELOAD_PAIRS=es-en,en-es`, multi-pair warm-up).

**Prep already done on the dev box** (commit this runbook's sibling artifacts):
English clips `samples/realtime-pcm-en/` + ladder `samples/telephony-pcm-en/`
(regenerate any time: `generate_en_es_samples.py` then `make_telephony_ladder.py --in … -en`).
All benchmarks below run **from the dev box** against the pod URL — only `websockets` needed.

---

## 0. Boot + warm-up proof (cold-start numbers come from here)

1. **Reset** (not just start) the stopped pod so it pulls the new `:latest`.
   Confirm `AUTH_TOKEN` is set in the pod env.
2. Watch the boot logs for:
   - `Warm-up: firing self-test through STT + MT/TTS pairs [es->en, en->es]...`
   - `Warm-up OK in …ms; node READY | pairs=[es->en, en->es] stages={…}` —
     the `translate_en_es` / `tts_es` numbers **are the reverse-leg cold-start cost**; record them.
   - If it logs `Warm-up FAILED … missing MT/TTS success for en->es`, the es voice or
     en→es model is broken in the image — stop here, that's the result.
3. Confirm `https://<pod>-8000.proxy.runpod.net/health` → 200 and `/metrics` shows
   `linguabridge_ready 1`, `warmup_stage_ms{...}` per stage.
4. Note GPU memory with both directions loaded: `nvidia-smi` on the pod
   (council asked for the delta vs the ~es-en-only baseline from S1 — record the number).

```powershell
# dev box env for all steps below
$env:SERVER_URL = "wss://<podid>-8000.proxy.runpod.net/ws/translate"
$env:AUTH_TOKEN = "<the pod's token>"
$py = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
```

## 1. es→en regression (must not degrade — council condition)

```powershell
& $py scripts/benchmark_s1_telephony.py --runs 3
```
Gate: same as S1 — mulaw8k P95 < 2000ms, WER ≤ 8%. Compare against
`runs/s1-telephony-20260605-233719.json` (P50 582ms / P95 897ms / WER 0.0%).

## 2. en→es reverse leg (the new numbers)

```powershell
& $py scripts/benchmark_s1_telephony.py --runs 3 --src en --tgt es `
    --manifest samples/stage2-en-es.json `
    --clean-dir samples/realtime-pcm-en --ladder-dir samples/telephony-pcm-en
```
Same gates apply (clean ≤3% / mulaw8k ≤8% WER, P95 < 2000ms). WER here measures
**English STT**; translation quality is step 4.

## 3. Alternating directions (direction-switch cost)

```powershell
& $py scripts/benchmark_s2_alternating.py --rounds 3
```
Gate: both directions P95 < 2000ms while interleaved. A big gap vs steps 1–2
means direction switching (model-cache/voice swap) costs latency — investigate
before calling bidirectional validated.

## 4. Spanish output quality (listen test)

The benchmarks discard TTS audio. Listen to the actual Spanish output with the
existing test client against the pod, en→es, using a couple of
`samples/realtime-pcm-en` clips; compare against `expected_translation_es` in
`samples/stage2-en-es.json`. Judge: intelligible? natural enough for a CS call?
(`es_ES-mls_10246-low` is a low-quality tier voice — if it sounds robotic, that's
a known candidate finding, log it, don't polish this sitting.)

## 5. Wrap up (same sitting, before stopping the pod)

1. **Stop the pod.**
2. Commit the new `runs/s1-telephony-*.json` (both directions) +
   `runs/s2-alternating-*.json` (`git add -f`, runs/ is gitignored).
3. Log the session spend:
   `& $py scripts/log_cost.py add --provider runpod --usd <hrs × 0.26> --note "bidir validation sitting"`
   — and fill the older unpriced ledger entries from the RunPod billing page.
4. Update `PROJECT_STATUS.md` S6 "Still unproven" line with the measured results.

## Decide

| Outcome | Next |
|---|---|
| All pass | Bidirectional validated → S2 (4-stream load test → real `MAX_SESSIONS`), S3 (token-broker + TLS) before any real audio |
| en→es latency fails | Profile `stage_ms` in the run JSON — if `tts_es` dominates, try a medium-tier es voice; if `translate_en_es`, check the MT cache warm path |
| en→es WER fails | English telephony STT is the issue — same levers as S1 (Whisper medium, VAD tuning) |
| Alternating fails but single-direction passes | Direction-switch cost — check model_cache eviction + Piper voice reload between turns |
| Spanish voice unusable | Log it; voice upgrade is a follow-up decision (size/latency trade-off), not this sitting |
