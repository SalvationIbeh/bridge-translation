# Realtime Translation Probe Results

**Date:** May 22, 2026
**Model:** `gpt-realtime-translate`
**Target language:** English
**Input type:** Five short synthetic Spanish PCM clips streamed in 100 ms chunks

## What Passed

- The dedicated translation WebSocket accepted live-paced PCM audio chunks.
- Translated audio deltas arrived before each source clip finished streaming.
- The probe produced translated WAV output plus transcript/event artifacts per
  case under `runs/realtime-translation`.

## Latest Probe

| Metric | Short-set p50 |
|---|---:|
| First translated audio delta | 1738.31 ms |
| First output transcript delta | 3245.84 ms |
| Translation session closed | 8842.56 ms |

| Case | Source audio | First audio delta | First transcript delta |
|---|---:|---:|---:|
| Card block | 3700 ms | 2286.77 ms | 2799.09 ms |
| Balance check | 3350 ms | 2172.51 ms | 3245.84 ms |
| Payment rejected | 3800 ms | 1738.31 ms | 3485.85 ms |
| Transfer pending | 3500 ms | 1507.53 ms | 4774.54 ms |
| Card arrival | 3700 ms | 1689.82 ms | 3049.13 ms |

The machine-readable report is `runs/realtime-translation-report.json`.

## Interpretation

This is a materially better latency path than the batch STT -> MT -> TTS chain.
The important signal is not the closed-session time; it is that translated audio
starts during the incoming utterance instead of after three sequential batch API
calls finish.

This is not a product latency pass yet. The current short-set p50 to first
translated audio is still above the original 800 ms p50 v1 target.

## Quality Notes

- Most translated outputs preserved the short support intent.
- The synthetic `transfer_pending` input produced a bad source recognition and
  bad translated transcript in this run.
- Transcript deltas in the report are useful for inspection, but audio quality
  remains the more important output for this speech-to-speech path.

## Next Experiment

Move from synthetic PCM fixtures to a one-way live microphone test:

1. Speak Spanish into the local test harness.
2. Hear English translated audio while speaking.
3. Measure live time to first translated audio with human speech.
4. Keep the test local and one-directional before adding call routing.

