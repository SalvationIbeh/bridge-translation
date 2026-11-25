# Stage 1 Spike

## Goal

Prove the AI translation chain before building the desktop app or virtual audio driver.

The initial implementation is file-based because provider quality and latency should be measured before OS audio routing becomes part of the problem.

## Scope

In scope:

- Spanish speech or mock transcript in.
- English translation out.
- English TTS audio artifact in real mode.
- JSONL latency logs per stage.
- No customer data.

Out of scope:

- Live microphone capture.
- VB-Cable / virtual audio routing.
- Bidirectional calls.
- Desktop UI.
- Banking system integration.
- Storage of audio, transcripts, or customer data.

## Passing Criteria

- Mock mode runs on a clean machine with no API keys.
- Real mode accepts a short Spanish test WAV and produces English audio.
- Logs contain separate timings for STT, translation, and TTS.
- Test phrases are fake CS/banking utterances, not customer data.

## Next Step

After the file benchmark passes quality checks, compare direct realtime
translation with a stitched streaming STT -> MT -> TTS path before adding live
local audio capture and VB-Cable routing on Windows.

## Troubleshooting

If real mode reaches the OpenAI translation step and reports `insufficient quota`,
the API project behind `OPENAI_API_KEY` needs API billing/credits before the
spike can translate. API billing is separate from a ChatGPT subscription.

Real mode defaults to OpenAI TTS:

```powershell
python .\scripts\run_stage1.py --mode real --audio-file .\samples\spanish_test.mp3 --output .\runs\stage1-output.mp3
```

To compare ElevenLabs after adding ElevenLabs credentials:

```powershell
python .\scripts\run_stage1.py --mode real --tts-provider elevenlabs --audio-file .\samples\spanish_test.mp3 --output .\runs\stage1-elevenlabs.mp3
```

## Short-Utterance Benchmark

The first real file run proves provider quality. It is a batch path, so a long
utterance pays for a full transcript, full translation, and full TTS file before
the output is playable.

Use the benchmark runner to measure a small fake CS set repeatedly:

```powershell
python .\scripts\benchmark_stage1.py --mode mock
python .\scripts\benchmark_stage1.py --mode real
```

The real benchmark reads `samples/stage1-benchmark.json`, writes per-case output
under `runs/stage1-benchmark`, and writes the summary report to
`runs/stage1-benchmark-report.json`.

`spanish_test.mp3` is included as a long baseline. Record the short files listed
in `samples/README.md` before treating the short-group p50 as meaningful.

To generate repeatable synthetic Spanish clips for timing experiments:

```powershell
python .\scripts\generate_stage1_samples.py
python .\scripts\benchmark_stage1.py --mode real
```

Synthetic clips are useful for fast latency iteration. They do not replace human
recordings for speech-recognition quality checks.

The latest measured benchmark result is recorded in
`docs/STAGE1_BENCHMARK_RESULTS.md`.

## Realtime Translation Probe

The realtime translation probe streams short Spanish PCM fixtures into
`gpt-realtime-translate` and records first transcript/audio delta timing:

```powershell
npm.cmd install
node .\scripts\probe_realtime_translation.mjs --case card_block
node .\scripts\probe_realtime_translation.mjs
```

The first run generates missing PCM fixtures from the fake benchmark phrases.
Translated WAV output and event logs are written under
`runs/realtime-translation`.

The latest realtime result is recorded in
`docs/REALTIME_TRANSLATION_PROBE_RESULTS.md`.

## Live Mic Probe

The local live mic harness uses browser WebRTC for a one-way human speech test:

```powershell
npm.cmd run probe:live
```

Open `http://127.0.0.1:8787`, start the mic session, arm one phrase, and speak
a fake Spanish support utterance. The page measures from detected microphone
speech to translated audio/transcript output.

See `docs/LIVE_MIC_PROBE.md`.
