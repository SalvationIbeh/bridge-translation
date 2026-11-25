# Council Review: Provision en->es Models in the Stage 2 GPU Image

**Date:** 2026-06-09

## Question

Should Bridge Stage 2 provision bidirectional English-to-Spanish support in the existing self-hosted GPU server image by adding `Helsinki-NLP/opus-mt-en-es` and an `es_ES` Piper voice to `server/Dockerfile`, while preserving the validated `es->en` path?

---

## Advisor Views

**Skeptic**

**Core assessment:** Adding en->es by baking another MT model and Piper voice is directionally reasonable, but it risks mistaking "model present in image" for "validated reverse-leg call path." The real decision is not dependency addition; it is whether this keeps the spike honest or quietly expands scope.

**Key concerns:**
- Latency symmetry is assumed, not proven. es->en at P50 582ms / P95 897ms does not imply en->es will match.
- Image bloat may create operational drag through slower provisioning and larger cold-start surface.
- Warm-up coverage can hide first-call failure if only the warm path is tested.
- Quality risk is underweighted; reverse-leg errors are customer-facing and more damaging.
- Scope creep is nearby if this turns into voice polish or demo readiness.

**Recommendation:** Conditionally support only if this is treated as a measured reverse-leg experiment, not platform expansion. Require separate cold/warm latency metrics, GPU memory reporting, reverse-leg quality checks, no transcript logging, and rollback if image size or P95 materially worsens.

**Product**

**Core assessment:** Adding en->es support directly serves the end-user promise: a real bilingual call requires both parties to hear translated speech, not just the agent side. This is the right kind of Stage 2 work because it validates the full bidirectional experience without drifting into desktop/productization.

**Key concerns:**
- Latency risk is the main product risk; en->es needs separate warm-up, cold-start, and telephony benchmarks.
- Voice quality matters because a poor Spanish voice can make the customer side feel robotic or untrustworthy.
- Docker image bloat is acceptable only if warm-up improves reliability without degrading es->en.
- Complexity is proportional to the stage because it tests whether a two-way translated call feels usable.
- Cost per unit of value is favorable if the models are baked once and reused.

**Recommendation:** Support, conditioned on benchmark gates. Add `opus-mt-en-es` and an `es_ES` Piper voice only with explicit reverse-leg warm-up coverage, no transcript/audio logging, and side-by-side latency reporting for both directions.

**Security**

**Core assessment:** Adding the reverse en->es model and Spanish Piper voice is security-acceptable if it remains inside the existing self-hosted GPU image and preserves the current no-content-logging posture. The main risk is expanding the bidirectional voice data surface without tightening retention, artifact provenance, and runtime isolation.

**Key concerns:**
- Both legs now process potentially sensitive customer and agent speech; logs, crash dumps, benchmark artifacts, and temp files must not retain content.
- Pulling model/voice assets introduces model artifact integrity concerns; prefer pinned sources and hashes over mutable URLs where feasible.
- Bidirectional TTS can synthesize Spanish speech, so output control is a sensitive capability if the server is compromised.
- A compromised server could access live audio streams, env secrets, model files, and operational logs.
- Bidirectional calls move the project closer to regulated call handling; consent and data minimization remain gates before real customer audio.

**Recommendation:** Support with conditions. Proceed only with synthetic warm-up content, no audio/transcript content logging or persistence, pinned/provenance-noted artifacts, and runtime hardening before real call flows.

**Optimizer**

**Core assessment:** Adding `opus-mt-en-es` plus an `es_ES` Piper voice is the fastest path to real bidirectional validation because it reuses the current architecture, avoids external latency/cost, and gives warm-path coverage for both call directions. The main risk is whether dual MT models and Piper voices stay warm under turn switching and concurrent calls.

**Key concerns:**
- Measure cold start, first-token/first-audio, and warmed steady state separately for `en->es`.
- Expect larger Docker image and longer provisioning, but near-zero marginal inference cost.
- Bottleneck likely remains Whisper or Piper depending on voice runtime behavior.
- Single-call latency is not enough; test alternating directions and 2-4 simulated concurrent calls.
- Add explicit warm-up for both MT directions and both Piper voices with metrics-only logs.

**Recommendation:** Support conditionally. This is the simplest solution that delivers most of the bidirectional-call validation value without changing providers or architecture. Gate merge/deploy confidence on warmed `en->es` P50/P95, cold-start cost, and alternating-direction latency.

---

## Synthesis

All four advisors support the change as a Stage 2 experiment, not a product expansion. The shared condition is measurement: `en->es` must earn its own latency and quality numbers, and the extra assets must not degrade the already-proven `es->en` path. Security's strongest addition is that all warm-up and benchmark flows must remain synthetic and metrics-only until S3/TLS/token-broker gates are complete.

## Dissent

None against proceeding. The Skeptic's dissent is about framing: baked assets are not proof of reverse-leg viability.

## Decision

**PROCEED WITH CONDITIONS**

Bake `opus-mt-en-es` and `es_ES-mls_10246-low` into the image and preload `es-en,en-es` at boot. Treat the result as unvalidated until reverse-leg warm/cold latency, alternating-direction latency, GPU memory impact, and Spanish output quality are measured on the RunPod GPU.
