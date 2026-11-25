# Decision: PCI-Avoidance Strategy for Spoken Card Numbers (§8 condition 4)

**Date:** 2026-06-14
**Status:** Decided (Council, unanimous) — ratification by founder pending
**Gate:** STAGE2_DESIGN §8 condition 4 — "two-party consent + PCI-avoidance plan
(mute/redact spoken-card segments)" before ANY real customer audio.

---

## Question

How should Bridge detect and mute/redact spoken card numbers (PAN/CVV/expiry) in the
live voice-translation pipeline (VAD → Faster-Whisper STT → Opus-MT → Piper TTS,
turn-based, <2s) so cardholder data never reaches STT output, MT, logs, or the
translated audio — without blowing the latency budget or the zero-persistence (S4)
guarantee?

Options considered:
- **(a)** regex/Luhn detection on the STT transcript post-hoc.
- **(b)** real-time number-sequence detector on audio/partial transcript, mute before finalize.
- **(c)** agent-triggered "pause/redaction" control flagging card-entry segments.
- **(d)** scope-out: contractually forbid spoken card numbers + consent + agent training.

---

## Council Report

### 🔴 Skeptic
Reject (b) as a v1 control — the contact-center industry spent a decade concluding
speech-based PAN detection can't be made PCI-reliable; mature players use DTMF
suppression. False-negative math is unforgiving (PCI doesn't grade on a curve; a
98%-accurate detector leaks ~1 in 50 PANs). The detector inherits Whisper's
telephony/Spanish digit errors — you can't redact what STT didn't recognize as
digits. Muting before finalization adds lookahead latency. Adopt (d)+(c) + route card
capture out-of-band (DTMF/IVR); treat speech redaction as research, never a control.

### 🟢 Product
Real-time redaction now is the wrong complexity for a stage still proving
latency/quality — pilots use synthetic PII, so there's no real CHD to redact yet.
Real contact centers *avoid* spoken cards (DTMF suppression / warm-transfer to a
payment line); the card never enters the voice stream. (a) fails the gate, (b) is a
latency trap. Ship (c)+(d): an agent "pause translation" toggle that hard-mutes
STT/MT/TTS/logging, mirroring the IVR-pause motion agents already know. Defer (b) to
post-validation hardening only on customer compliance demand.

### 🔵 Security
Process-then-redact ((a)/(b)) pulls Bridge INTO PCI-DSS scope as a CDE component (PCI
scopes anything that stores, processes, *or transmits* CHD), requiring SAQ-D controls
Bridge can't yet meet. A missed PAN under zero-persistence still transits RAM, the MT
step, and the translated audio to the far end — zero-persistence stops disk, not
transmission. CVV must never be retained even transiently. Consent-law variance
(two-party states, GDPR) applies to the whole call. Defensible pre-revenue minimum:
keep Bridge categorically OUT of the card-data path. "Bridge does not process
cardholder data" is defensible; "processes and redacts it" is not.

### 🟡 Optimizer
Bridge is turn-based, so (b) doesn't exist without re-architecting to streaming STT
(weeks of work; erodes GIL-bound MAX_SESSIONS=4 headroom), and (a) leaks PAN into
process memory before redaction fires. Cheapest/safest redaction lives at the client:
agent toggles a control message, the client drops/zeroes audio frames at source for
flagged segments → PAN never reaches server-ingress, STT, MT, TTS, or logs. 0ms added
latency, 0 per-session compute, 0 detection risk. Reframe (c) as client-side
"redaction mode"; layer (d) underneath.

### Synthesis
Unanimous: reject (a) and (b) as PCI controls — both put CHD inside Bridge's process,
pulling it into PCI scope, and can't be made reliable in a turn-based, telephony-
Spanish ASR pipeline. All four endorse a **descope strategy**: Bridge must never be in
the card-data path. Mechanism: agent-triggered "redaction mode" (client drops frames;
server enforces by discarding inbound audio pre-VAD/STT while active), backed by
contractual prohibition + two-party consent + agent training, with real payment
capture routed to a separate PCI-compliant channel (DTMF/IVR).

### Dissent
None — unanimous. Nuance only: Optimizer locates the cut client-side (zero-cost);
Security stresses payment moving fully off-channel. Complementary layers.

---

## Decision

**CONDITION → PROCEED with descope (c-reframed + d). Reject (a) and (b).**

The §8.4 PCI-avoidance plan is: **Bridge does not process cardholder data.**

1. **Redaction mode (technical):** an agent-triggered control message starts/ends a
   redacted segment. The client SHOULD drop/zero audio frames at source (zero added
   latency). The **server enforces** it regardless: while redaction is active it
   discards inbound audio frames *before* VAD/STT, so nothing is transcribed,
   translated, synthesized, or logged. Defense-in-depth even if the client misbehaves.
2. **Off-channel payment (architectural):** actual card capture moves to a separate
   PCI-compliant channel (DTMF-to-gateway / IVR pause-and-resume). Bridge is never the
   capture path.
3. **Contractual + consent + training (d):** spoken card numbers contractually
   prohibited; two-party consent disclosed for the whole call; agents trained to
   trigger redaction mode and use the payment channel.
4. **Out of scope for v1:** speech-based automatic PAN detection ((a)/(b)). It is a
   research item, never a compliance control, and must never gate real customer audio.

**Buildable now (off-pod):** the server-side redaction-mode control frame + its
enforcement in the WS audio loop, plus this plan document. The client-side frame-drop
waits for the desktop client (Stage 2 build, out of current scope).
