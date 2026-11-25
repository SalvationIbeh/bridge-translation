# Decision: Opus-MT vs MADLAD-400 for Multi-Language Translation

**Date:** 2026-06-01
**Status:** CONDITION — stay on Opus-MT until conditions are met

---

## Question

Which translation model should Bridge use for multi-language support at scale: Opus-MT
(per-pair, ~300MB each) or MADLAD-400 (all-pairs, ~10GB, Apache 2.0)?

## Context

- Real-time call translation, 20 concurrent sessions, RTX PRO 4500 (~16GB VRAM)
- Current P50: 3,221ms end-to-end with Opus-MT es→en
- Target: sub-2,500ms P50
- Roadmap: 10+ language pairs eventually

## Council Decision

**CONDITION — Stay on Opus-MT until:**

1. **Pipeline profiled** — instrument each stage (STT, MT, TTS, queue wait). If MT < 800ms
   of the 3,221ms total, model choice is not the bottleneck.
2. **MADLAD-400 INT8 benchmarked** — run at 20 concurrent sessions on the actual RTX PRO
   4500 pod, with realistic 8–15 word turn lengths. Must clear 2,500ms P50 under full load.

## Key Findings

- Three of four advisors favor MADLAD-400 long-term for operational simplicity at scale
- Skeptic and Optimizer both flag: bottleneck is unattributed — MT may not be the problem
- Security requires three controls before MADLAD-400 production deployment:
  1. Session-scoped batching (no cross-session KV-cache bleed)
  2. Explicit GPU memory scrubbing on session close
  3. Signed model artifact provenance check at container build time
- Single-point-of-failure risk: MADLAD-400 outage kills all language pairs simultaneously

## Next Actions

- [ ] Add per-stage timing instrumentation to server pipeline
- [ ] Run 20-concurrent load test and record MT-only latency
- [ ] If MT > 800ms: benchmark MADLAD-400 INT8 on pod
- [ ] If MADLAD-400 passes: implement 3 security controls before shipping
