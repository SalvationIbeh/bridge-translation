# Stage 1 Benchmark Results

**Date:** May 22, 2026
**Path tested:** Deepgram prerecorded STT -> OpenAI text translation -> OpenAI TTS
**Input type:** One human-recorded long sample plus five synthetic short Spanish clips

## What Passed

- The real Spanish-to-English AI chain produced good output audio.
- The repeatable benchmark runner generated per-case reports and latency logs.
- Short synthetic utterances completed materially faster than the long baseline.

## Latest Real Benchmark

| Group | Cases | p50 total latency |
|---|---:|---:|
| Long baseline | 1 | 17678.63 ms |
| Short utterances | 5 | 4828.21 ms |

Short-case totals:

| Case | STT | Translation | TTS | Total |
|---|---:|---:|---:|---:|
| Card block | 1361.06 ms | 2617.70 ms | 1340.88 ms | 5319.64 ms |
| Balance check | 1707.11 ms | 1616.51 ms | 1193.84 ms | 4517.46 ms |
| Payment rejected | 1274.00 ms | 975.55 ms | 1603.37 ms | 3852.92 ms |
| Transfer pending | 1514.94 ms | 1233.82 ms | 2079.45 ms | 4828.21 ms |
| Card arrival | 1246.22 ms | 2717.20 ms | 1367.33 ms | 5330.75 ms |

The machine-readable report is `runs/stage1-benchmark-report.json`.

## Interpretation

This batch file path is not a latency pass. Every case waits for a full STT
response, then a full text translation, then a complete TTS file.

The next latency spike should compare two narrower paths:

1. OpenAI realtime translation for direct streaming speech-to-speech output.
2. A provider chain with streaming STT, text translation, and early TTS start.

The comparison should answer:

1. How quickly does the first translated transcript or audio delta arrive?
2. How quickly does streaming STT return the first useful Spanish partial?
3. How quickly does either path reach a stable utterance boundary for a short turn?
4. Can the stitched provider chain start translation and TTS before the full
   source file is complete?

## Caveats

- The five short clips are synthetic OpenAI TTS inputs. They are useful for
  repeatable timing, but not for judging human accents or noisy caller audio.
- This benchmark measures API round trips and batch processing, not mouth-to-ear
  live-call latency.
