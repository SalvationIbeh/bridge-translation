# Decision — Bridge Stage 2 v1 Architecture

**Date:** 2026-06-04
**Method:** Council review (4 independent advisors: Skeptic / Product / Security / Optimizer)
**Feeds into:** `docs/STAGE2_DESIGN.md`
**Status:** CONDITION (proceed with conditions)

---

## Question

Stress-test the Bridge Stage 2 v1 architecture before it goes into the Stage 2 design doc. Five decisions:

1. **v1 translation stack** — self-hosted GPU (Whisper-small / Opus-MT / Piper) vs cloud-buy (Deepgram / OpenAI / ElevenLabs) vs hybrid.
2. **Compute platform** — Modal serverless vs RunPod persistent pod vs hybrid warm-standby.
3. **Deployment topology** — number of always-warm T4s, concurrency per T4, scale-on-demand trigger.
4. **Desktop(Rust)↔GPU-server WebSocket protocol** — reintroduces our infra into the audio path, reversing TRD v0.2 §4.6 "no audio proxy."
5. **Multi-language model loading** — Opus-MT ~3.9s load each; pre-load all vs on-demand vs warm-pool.

**Stage 1 evidence:** self-hosted GPU stack ~550ms P50 / WER 3.3% (clean audio, RunPod RTX); cloud chain ~2,035ms P50, failed P95. Modal cold-start 29s / 413ms warm; pre-warm mandatory; 1 always-warm T4 ≈ $14/day. TRD v0.2 (pre-spike) committed to cloud-buy and "backend does NOT proxy audio."

---

## Council Report

### Advisor Views

**🔴 Skeptic**

*Core assessment:* The spike validated a tech chain under lab conditions, not a product decision under real conditions — and the team is using a clean-audio latency win to justify reversing a deliberate architectural principle (no audio proxy) and taking on GPU ops it has no reason to own at one user.

*Key concerns:*
- The 550ms is a lab number, measured on synthetic/clean audio. Real CS calls add μ-law 8kHz codec loss, packet jitter, crosstalk, double-talk, silence-detection ambiguity. The sample ladder predicts WER jumping 3.3% → 8–12%. Latency *variance* (P95), not P50, breaks UX — and P95 under jitter was never measured. The cloud chain *did* fail P95; assuming self-hosting won't is unproven.
- Self-hosting trades a solved problem for an unsolved one. Cloud providers handle autoscaling, redundancy, codec robustness, 24/7 uptime. A solo builder running an always-warm T4 owns paging, GPU OOM, CUDA breakage, model reloads, SPOF — for one user. That's a second job, not leverage.
- The cold-start math is fragile. "1 warm T4 ≈ $14/day" assumes one warm instance suffices; concurrency-per-T4 under real load is unmeasured. User #2 and #3 overlapping a call → queue (latency spike) or scale (29s cold = dropped turn).
- Reversing "no audio proxy" is the expensive, hard-to-undo decision: a network hop, a protocol to maintain, customer-audio liability v0.2 explicitly avoided.

*Recommendation:* **Condition, don't adopt.** At 1–4 users ship the cloud chain (managed, no ops) and fix P95 by tuning endpointing/streaming. Gate any GPU stack behind a telephony-codec + jitter re-benchmark showing P95 < 2000ms under degraded audio.

**🟢 Product**

*Core assessment:* The self-hosted GPU stack is the only chain that actually hits the latency and WER targets, so it wins on product experience — but for a 1→4 user pilot, standing up always-warm GPU infra is premature operational weight that steals time from the real moat (audio reliability and UX). Right call on the model stack, wrong stage to over-engineer the platform.

*Key concerns:*
- The customer only cares that the call works. 550ms vs 2,035ms is conversation vs walkie-talkie. Cloud failing P95 is a trust-killer — one bad turn per call erodes confidence faster than ten good ones build it.
- $14/day for one always-warm T4 ≈ $420/mo to serve 1–4 users is a fine pilot learning cost, but it's a latency-buying expense, not a unit-economics signal. Defer the multi-T4 / concurrency / scale-trigger design — no load to scale.
- Infra-as-distraction is the real risk: cold-starts, WebSocket reconnects, pre-warm cron = time NOT spent on barge-in, turn-detection, voice naturalness, desktop failure recovery.
- Reversing v0.2's "no audio proxy" is acceptable but must be bounded — now a product-trust commitment, not just architecture.
- Multi-language loading is a non-problem: v1 is one pair. Pre-load es↔en, delete the question.

*Recommendation:* **Condition.** Adopt the self-hosted stack — it's the only one that delivers the product. Platform: choose **one persistent RunPod pod**, not Modal serverless, not a warm-standby fleet — lowest-ops way to keep a GPU hot for 4 users. Defer Decisions 3 and 5. Bound Decision 4 with a written data-handling note. Spend the saved time on reliability and UX.

**🔵 Security**

*Core assessment:* Self-hosting reverses the TRD v0.2 security model — raw CS call audio containing PII (and likely PCI cardholder data) now traverses the public internet to *your* GPU process on a third-party host (Modal/RunPod), making you data controller and processor with none of the SOC 2 / BAA / PCI scaffolding the cloud providers already carry. Neither path is "private"; self-hosting trades a vetted processor for a DIY one you must secure yourself.

*Key concerns:*
- PCI/PII in audio at rest and in transit. Spoken card numbers and balances flow over the WebSocket and into VRAM, Whisper buffers, debug/transcript logs. The "no transcript logging" rule must be enforced everywhere incl. crash dumps and the platform's own logs. Card audio likely drags you into PCI DSS scope — a solo builder cannot realistically certify that.
- Public WebSocket attack surface. An unauthenticated endpoint accepting raw audio is a free GPU/transcription oracle and DoS target. Need mTLS or short-lived signed tokens (keep the token-broker pattern), per-connection authz, origin pinning, rate limits, message-size caps.
- The GPU host is a third party. Modal/RunPod can snapshot VRAM, access disk, read logs. "Self-hosting" on rented GPUs still needs a DPA, region pinning (US/EU for GDPR), disk/secret encryption — without a BAA-grade contract you've moved the trust, not removed it.
- Compromise blast radius: live audio in VRAM, buffered chunks on disk, secrets in env. Mandate ephemeral storage, no swap, zeroize buffers, no audio persistence, metadata-only logs.
- Two-party consent / GDPR: recording+processing CS calls needs documented consent from both parties and a lawful basis; es↔en implies EU data subjects.

*Recommendation:* **Condition, don't reject.** Pilot only on the founder's own/wife's consented calls with synthetic PII until: (1) TLS + token-broker authn on the WS, (2) zero audio/transcript persistence proven, (3) signed DPA + region pinning with the GPU host, (4) explicit two-party consent + a PCI-avoidance plan (mute/redact card segments). No real third-party customer audio before all four are true.

**🟡 Optimizer**

*Core assessment:* The architecture is sound and already hits target (~550ms P50, WER 3.3%). At 1→4 users the entire question collapses to "one persistent warm T4," and Modal's serverless auto-scale buys you nothing because you must keep one warm 24/7 regardless — you'd pay $14/day AND eat 580ms RPC overhead. RunPod persistent pod is the simpler 90%-value answer.

*Key concerns:*
- TTS is the inference bottleneck (Piper ~444ms of ~550ms). This, not platform choice, is the real latency lever — stream/chunk Piper output if you ever need to cut latency; don't re-platform.
- Serverless is the wrong tool. Modal shines for spiky, idle-heavy workloads. A live call holds the GPU for its duration and you can't tolerate 29s cold starts mid-pilot, so you pin one warm anyway — buying serverless, using it as a persistent pod, plus 580ms RPC tax. Reject Modal for v1.
- Concurrency: one streaming call is light on a T4 (small Whisper + Opus-MT + Piper fit in <6GB, compute-bound only during active speech). Realistically 4–8 concurrent calls per T4; 4 pilot users fit on ONE T4. Validate with a 4-stream load test.
- Multi-language: pre-load all needed Opus-MT pairs at boot (3.9s is one-time, small VRAM each). On-demand load injects 3.9s into a live call — unacceptable. Warm-pool only matters past ~10 pairs.

*Recommendation:* **Condition.** Ship v1 on a single RunPod persistent warm T4 (~$14–17/day flat; beats per-API cloud above a few hundred call-minutes/day). Keep the Rust↔server WebSocket generic (opaque audio frames + language tags) so swapping compute later is free. Scale trigger: add a second pod when sustained concurrent calls exceed 4. Don't touch serverless or warm-pools until real multi-tenant load — that's Stage 3.

---

### Synthesis

All four advisors endorse the **self-hosted GPU model stack** — it is the only chain that hits the latency and WER targets, and the experiential gap over the cloud chain (≈550ms vs ≈2,035ms, plus the cloud P95 failure) is decisive for product trust. The strongest convergent finding, raised independently by **Product and Optimizer**, is that the compute platform should be a **single RunPod persistent warm T4, not Modal serverless**: because pre-warm is mandatory, serverless auto-scale delivers no benefit and adds ~580ms of RPC overhead per call. This overrides the prior "Modal wins" decision in PROJECT_STATUS. Both also agree Decisions 3 (scale topology) and 5 (multi-language loading) are non-problems at the 1→4-user / single-pair stage and should be deferred. Skeptic and Security supply the binding conditions: the 550ms is a clean-audio lab number that must be re-validated under telephony codec + jitter (P95 gate), and reversing "no audio proxy" demands an authenticated, non-persisting, DPA-backed audio path before real customer audio flows.

### Dissent

**Skeptic** is the lone "don't self-host yet" voice: at 1–4 users, ship the managed cloud chain (zero ops) and fix its P95 by tuning endpointing/streaming; only earn the GPU stack by passing a degraded-audio P95 < 2000ms re-benchmark. Preserved because the core risk is real — the latency win is unproven under μ-law 8kHz + jitter + crosstalk, which is exactly where the cloud chain failed.

### Decision

**CONDITION — proceed with the self-hosted GPU stack on a single RunPod persistent warm T4, conditions attached.**

1. **Translation stack:** self-hosted GPU (Silero VAD → Faster-Whisper small → Opus-MT → Piper) is the v1 pipeline. Cloud chain demoted to documented fallback.
2. **Compute platform:** single **RunPod persistent warm T4** for v1 — **not** Modal serverless (serverless value nullified by mandatory 24/7 pre-warm; adds RPC latency). *Overrides the Modal decision in PROJECT_STATUS — surfaced for founder sign-off.*
3. **WebSocket protocol:** generic (opaque audio frames + language tags) and authenticated (token-broker from v0.2, TLS, message-size caps, rate limits) so the compute backend stays swappable and the audio path is bounded.
4. **Defer** scale-topology and multi-language loading to Stage 3. Pre-load es↔en at boot for v1.
5. **Gate before real customer audio:** (a) telephony-codec + jitter re-benchmark, P95 < 2000ms; (b) zero audio/transcript persistence proven; (c) signed DPA + region pinning with the GPU host; (d) two-party consent + PCI-avoidance (mute/redact card segments).

**Most important reason:** the self-hosted stack is the only measured path that delivers a conversational product, but every advisor independently rejected the operational and latency overhead of Modal serverless for a workload that must stay warm 24/7 — RunPod persistent is the lean, swappable choice for the 1→4-user stage.
