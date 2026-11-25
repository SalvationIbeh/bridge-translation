# Bridge — Quality Guardrails

> Authoritative operational standard for all work on this project.
> Treat as binding alongside `CLAUDE.md`. Read before every session. Check before every commit.

---

## Council Decision Logging (Mandatory)

Every `/council` review result **must** be recorded in `docs/decisions/YYYY-MM-DD-[slug].md`.
Do not ask — always record automatically after synthesizing the report.

---

## Hard Gates — These Block Progress

### Gate 1: Latency Benchmark Before Architecture Docs

**Rule:** No design doc (PRD, TRD, architecture decision) may be written or updated until
real latency numbers exist for every new component being designed.

**What counts as real numbers:**
- Measured on actual hardware (GPU pod, not laptop CPU)
- Minimum 5 warm runs (first run discarded as cold start)
- P50 and P90 recorded
- STT, MT, TTS measured individually AND as end-to-end pipeline

**Violation example:** Writing a TRD section assuming OpenVoice v2 synthesis = 220ms
without benchmarking OpenVoice v2 on the target GPU.

**Checklist before writing any latency budget:**
- [ ] Component benchmarked on RunPod GPU (not local machine)
- [ ] Cold start run discarded
- [ ] 5+ warm runs recorded
- [ ] P50 and P90 in PROJECT_STATUS.md
- [ ] Numbers reviewed and approved before doc is written

---

### Gate 2: Accuracy Check Before Model Upgrade

**Rule:** Any model swap (e.g. Whisper base → small, Opus-MT → MADLAD-400)
must include a transcription/translation accuracy spot-check on the 5 standard sample clips
before the swap is committed to the Dockerfile.

**Standard sample clips:** `samples/realtime-pcm/*.pcm` (5 clips, es→en)

**Pass criteria:**
- Zero transcription errors on proper nouns (names, product terms)
- No word substitutions that change meaning
- Translation natural enough to be understood by a non-native speaker

---

### Gate 3: No Stage Advance Without GPU Numbers

**Rule:** Stage N+1 design work does not begin until Stage N GPU test results are in
PROJECT_STATUS.md with real P50/P90 numbers.

Current stage gate: Full GPU P50 must be confirmed before Stage 2 TRD is written.

---

## Architecture Decision Rules

### Latency Budget

Always use measured numbers, never estimated. Current measured baselines (2026-06-01):

| Component | Measured (GPU) | Notes |
|---|---|---|
| Silero VAD | ~400ms | Silence detection window |
| Faster-Whisper small | ~320ms P50 | GPU, Gate 1 + Gate 2 passed 2026-06-01 |
| Opus-MT es→en | ~83ms | GPU, warm |
| Piper TTS en | **~444ms** | GPU, P50 warm on real CS audio (was 2,800ms CPU) |
| **End-to-end P50 (base, real CS audio)** | **~827ms** | es→en, 4 warm runs, 2026-06-01 |
| **End-to-end P50 (small, synthetic)** | **~550ms** | es→en, 5 warm runs, 2026-06-01 |
| **End-to-end P90** | **~892ms** | base reference |

Update this table whenever a component is swapped or upgraded.

### Model Selection

| Concern | Rule |
|---|---|
| License | Commercial safe only. MIT, Apache 2.0 allowed. CC-BY-NC **blocked**. |
| VRAM | Model must fit with headroom for 20 concurrent sessions |
| Accuracy | Must pass Gate 2 spot-check on standard clips |
| Latency | Must be benchmarked (Gate 1) before committing to design |

### Multi-Language Strategy

Opus-MT: language-pair specific. One model per pair.
MADLAD-400: all pairs in one model, higher VRAM, slower first inference.

**Decision pending** — benchmark both before TRD v3 is written.

---

## Pre-Commit Checklist

Before staging any change:

- [ ] Change tested on RunPod pod (not just local)
- [ ] Server logs checked — no ERROR or WARNING lines introduced
- [ ] If latency-sensitive: 5 warm runs recorded and compared to baseline
- [ ] If model swap: Gate 2 accuracy check passed
- [ ] No secrets in code (AUTH_TOKEN, API keys via env only)
- [ ] `PROJECT_STATUS.md` updated if numbers changed

---

## Definition of Done by Change Type

| Change Type | Done When |
|---|---|
| Bug fix | Test client runs clean, server logs no errors, gate checks pass |
| Model swap | Gate 1 + Gate 2 both pass, PROJECT_STATUS.md updated |
| New pipeline stage | Benchmarked end-to-end, latency budget table updated |
| Protocol change | test_client.py updated to cover new frame types, full test run passes |
| Architecture decision | Council review done (see `/council` skill), decision recorded in docs/ |
| Stage advance | Gate 3 satisfied, new stage design doc reviewed by council |

---

## High-Risk Areas

1. **AUTH_TOKEN** — never default, never hardcoded, always from env
2. **WebSocket frame handling** — malformed frames must not crash worker pools
3. **VRAM** — new models must be checked against total VRAM budget before adding
4. **Docker layer order** — expensive layers (model downloads) must come after cheap ones
5. **PID 1 in container** — never kill PID 1; always check `ps aux` before any kill command
6. **RunPod env vars** — template env vars require pod restart to take effect; verify with `echo $VAR`
7. **GPU compute type** — `float16` requires GPU; `int8` can run on CPU; mismatch = silent error
8. **cuDNN** — CTranslate2 requires `libcudnn8` for GPU inference; must be in Dockerfile apt-get

---

*Companion docs: `PROJECT_STATUS.md` · `LinguaBridge_Changes.md` · `Bridge_TRD_v0.2.md`*
