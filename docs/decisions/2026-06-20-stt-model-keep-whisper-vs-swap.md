# Decision: Keep faster-whisper (small) for STT — do not swap models now

**Date:** 2026-06-20
**Status:** DECIDED — REJECT swap now / CONDITION future swap on measured data
**Trigger:** Live desktop probe (2026-06-19/20) showed Whisper hallucinating on
silence/non-speech (invented "Subscribe!" / "Amara.org" / repetition loops — Whisper
was trained on scraped YouTube subtitles). Founder asked whether a better STT model
exists. Per project rule, any STT model swap auto-triggers a Council review.

## Question

Should Vocivio replace faster-whisper (Whisper small) as the STT model for real-time
ES↔EN call translation, or is the just-shipped server-side hallucination filter the
right call? Alternatives considered: Whisper medium/large-v3, distil-whisper, NVIDIA
Parakeet/Canary, faster-whisper + better VAD, wav2vec2, streaming ASR.

## Context at decision time

- Self-hosted GPU pod (RTX 4000 Ada 20GB / A6000 49GB, RunPod). No third-party STT
  API — audio never leaves the pod (deliberate no-egress + zero-persistence posture).
- Pipeline: mic → Silero VAD → Whisper small (faster-whisper, CT2, float16) → Opus-MT
  → Piper. Server E2E P50 ~550ms vs <2000ms target. ~2.1GB VRAM/process.
- Whisper small WER ~3.3% clean Spanish; bidirectional ES↔EN validated.
- Concurrency ceiling ~4 sessions/process is **Python-GIL bound, not GPU bound**.
- Already shipped (commits eb04551, b6aa39e, + phrase-cycle fix): no_speech/logprob
  gating + known-phrase denylist + exact phrase-cycle detection. Removed most but not
  all live hallucinations; some residual lines were **acoustic echo** (speakers→mic),
  not Whisper.

## Council Report

### Advisor Views (summary)

- **🔴 Skeptic — REJECT/condition:** Misattributed root cause (echo ≠ model). A new
  model fed the same garbage audio hallucinates differently, not less. Alternatives are
  English-first → regress validated ES↔EN; bigger Whisper threatens latency/VRAM. No
  measured residual hallucination rate exists to justify a swap. "The model is the
  problem" is the premature-optimization trap.
- **🟢 Product — REJECT/condition:** Hallucination is a real trust-killer, but the
  filter is the right-altitude fix for this stage. Bigger model still hallucinates on
  silence. 3.3%→~3% WER is invisible to users; swap costs scarce solo-founder weeks.
  Fix echo at the audio layer; instrument hallucination rate in real sessions.
- **🔵 Security — CONDITION:** Hallucinated transcript → fabricated translation →
  false call record = data-integrity/compliance risk (PCI mis-attribution, GDPR
  5(1)(d) accuracy). No-egress + zero-persistence is the crown jewel and must survive
  any swap. Filter stays regardless (it's the compliance control). Allow a swap only
  to a self-hostable, license-cleared (verify NeMo/Parakeet CC-BY/NVIDIA vs MIT
  Whisper), SHA-pinned + scanned model that demonstrably lowers measured hallucination.
- **🟡 Optimizer — REJECT/condition:** VAD/gating problem, not model. Larger Whisper
  hallucinates *more* confidently on silence. GIL-bound ceiling means a heavier model
  lowers concurrency and raises $/session. Tighter Silero VAD (raise threshold,
  min-speech ~250ms, min-silence ~300–500ms, ~200ms padding) + shipped filter = ~90%
  of value at ~zero VRAM/latency. Prefer Canary over larger Whisper only if VAD+filter
  provably can't close the gap.

### Synthesis

Unanimous: do not swap now. All four independently diagnosed the same root cause —
non-speech audio reaching Whisper (VAD gaps + acoustic echo) — which a different model
does not fix. Whisper-small WER is on-budget; alternatives are mostly English-first
(regressing bidirectional), threaten the latency margin and the GIL-bound concurrency
economics. Security's caveat: the swap is permissible **later, gated**, and the filter
must stay regardless as a compliance control.

### Dissent

None opposed keeping Whisper. Security frames it as CONDITION (future data-justified
swap not blocked) rather than flat reject — preserved.

## Decision

**REJECT the swap now. CONDITION any future swap on measured data.**

Keep faster-whisper small. Treat the hallucination root cause as the **audio front-end,
not the model.** Action order:

1. **Acoustic echo cancellation (AEC) + headset guidance** in the desktop client
   (see memory: acoustic-echo-aec-needed). Removes the speakers→mic feedback loop.
2. **Tune Silero VAD** — raise threshold, min-speech ~250ms, min-silence ~300–500ms,
   ~200ms speech padding — so non-speech stops reaching Whisper.
3. **Keep the shipped hallucination filter permanently** — it is the compliance control,
   independent of model choice.
4. **Measure** residual hallucination rate on a telephony + silence/noise sample ladder.

Only revisit a model swap if hallucinations survive *clean* input. Then evaluate
**NVIDIA Canary** (multilingual ES+EN) ahead of larger Whisper, under Security's gates:
self-hostable, license-cleared, SHA-pinned/scanned weights, no-egress + zero-persistence
intact, and a demonstrated WER/latency/hallucination win.
