# Decision: TTS audio wire format — parse WAV client-side, don't change the protocol

**Date:** 2026-06-20
**Status:** DECIDED (Council)
**Context:** Stage-2 desktop client build, Phase A (`.claude/plans/` Stage 2 Client v1 plan).

## Question

Should we change the `/ws/translate` wire contract to (1) add a `sample_rate` integer field to
the `transcript` JSON frame and (2) switch the binary TTS frame from a WAV container to headerless
little-endian int16 mono PCM — so the client stops guessing the playback rate by language
(`tts_rate_for()`) and stops misreading the 44-byte WAV header as a ~1ms click?

Current state: `server/main.py:194-209` sends a transcript text frame then `send_bytes(job.tts_audio)`,
where `tts_audio` is a full WAV container built in `server/pipeline/tts_worker.py:71-81`. The client
(`desktop/src/main.rs:66-71`, `audio.rs:25-30`) decodes it as headerless PCM (→ click) and guesses
the rate by target language (only en/es verified; 6 other Piper voices would play at wrong pitch).

## Council Report

**🔴 Skeptic — Reject the headerless-PCM swap; condition the rest.** Replacing self-describing WAV
with an implicit, ordering-dependent contract splits one logical message (audio + its rate) across
two frames. Reverse rolling-deploy gap: new client + old server still receives WAV with no
`sample_rate` — the fallback must sniff `RIFF` or it regresses. Rate belongs *on* the audio frame,
not adjacent to it. "Text-then-binary 1:1" is an assumption, not a guarantee (interleave, no-audio
transcript, error frames). WAV already carries the rate — the actual bug was mis-decoding, not WAV.
No protocol versioning exists.

**🟢 Product — Support, ride alongside the build.** Wrong-pitch is a hard blocker on the core value
prop, not polish: 6 of 8 voices ship broken → caps the product at 2 languages and undercuts the
"expand languages" story. Cheap alternatives are false economy *if* they leave the rate guess
unsolved. (Premise: assumed the rate was unrecoverable on the wire — which the WAV-parse path
falsifies.)

**🔵 Security — Support, conditioned.** Dropping client-side WAV parsing removes a classic
memory-corruption surface, but a client that blindly trusts a server-supplied rate is a
DoS/overflow primitive (absurd rate → resampler buffer-size overflow / OOM / div-by-zero).
Conditions: bounds-check rate (whitelist ~8000–48000), cap PCM length vs `MAX_FRAME_BYTES` + even-byte
(int16-alignment) check, pin rate per-utterance with no cross-utterance carryover. `sample_rate` is
metadata, safe to log; ensure error paths don't dump PCM/transcript.

**🟡 Optimizer — Reject the wire change; do the client-side WAV-header parse.** WAV is already
self-describing. ~10 lines, one file, one side: read rate from header bytes 24–27, strip the 44-byte
RIFF header, play the rest as int16 PCM. Fixes the click AND yields the correct rate for every
voice. Single source of truth (the audio bytes). No server change, no SDK coordination, no Council
gate. Only revisit a binary contract if/when moving to chunked/streaming TTS — and then use a 4-byte
LE rate header *prepended to the audio frame*, not a JSON field.

### Synthesis
3 of 4 converge: the wire change is unnecessary because **WAV already carries the rate** — the fix
is client-side. Product's "support" rests on a premise the WAV-parse path falsifies, so its real
concern (kill the rate guess, unblock all voices) is fully satisfied by the cheaper option.
Security's conditions on the rate value apply regardless of source.

### Dissent
Product's "self-describing on the wire" instinct is right long-term — preserved as: when we move to
chunked/streaming TTS, switch to headerless PCM with a 4-byte rate header (not JSON) + a
`protocol_version` handshake.

## Decision

**REJECT the proposed wire-contract change. Instead, parse the WAV header client-side.**

The client reads the sample rate from the WAV header the server already sends, strips the 44-byte
RIFF header, and plays the remaining int16 mono PCM. `tts_rate_for()` is deleted (or kept only as a
last-resort fallback if the `RIFF`/`WAVE` magic is absent). The server is unchanged — keep WAV on
the wire.

**Conditions (Security, binding):**
1. Bounds-check the parsed sample rate to a sane range (8000–48000 Hz); reject/clamp otherwise.
2. Validate `RIFF`/`WAVE` magic before trusting the header; cap PCM length against `MAX_FRAME_BYTES`;
   reject odd-length PCM (int16 alignment).
3. Rate is per-utterance; never carry a rate across utterances.

**Deferred (Dissent, for later):** if/when TTS becomes chunked/streaming, move to headerless PCM
with a 4-byte LE rate header on the audio frame plus an explicit `protocol_version` handshake.

## Consequences

- Phase A of the Stage-2 client plan is **no longer a wire-contract change** → no further Council
  gate needed for it, no rolling-deploy hazard, and it fixes all 8 voices (not just en/es).
- Net security improvement: a minimal bounded header-read replaces both the silent mis-decode and
  the language-guess; no full WAV parser is introduced.
