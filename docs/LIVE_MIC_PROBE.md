# Live Mic Probe

The one-way live microphone probe uses WebRTC for browser capture and translated
playback. The local Node server keeps `OPENAI_API_KEY` server-side and only
returns a short-lived translation client secret to the page.

## Run

```powershell
npm.cmd install
npm.cmd run probe:live
```

Open:

`http://127.0.0.1:8787`

## First Test

1. Start the mic session.
2. Arm a phrase.
3. Speak a short fake Spanish support utterance.
4. Listen for English translated audio.
5. Read the first translated audio and transcript timings.

Suggested fake phrase:

`Quiero bloquear mi tarjeta por un cargo extrano.`

## Scope

This harness is one-directional and local:

- Source: local microphone speech.
- Output: translated remote audio track and transcript deltas.
- Storage: the harness does not write microphone audio or live transcripts to
  disk.

The next step after this probe is a local two-end test, not product UI work.
