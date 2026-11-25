from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bridge_spike.factory import DEFAULT_MOCK_TRANSCRIPT, build_stage1_pipeline, build_streaming_pipeline
from bridge_spike.latency import LatencyLog
from bridge_spike.providers import ProviderError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Bridge Stage 1 translation spike.")
    parser.add_argument("--mode", choices=["mock", "real"], default="mock")
    parser.add_argument("--tts-provider", choices=["openai", "elevenlabs", "kokoro"], default="openai")
    parser.add_argument("--audio-file", type=Path, default=Path("samples/mock-input.wav"))
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--latency-log", type=Path, default=Path("runs/stage1-latency.jsonl"))
    parser.add_argument("--mock-transcript", default=DEFAULT_MOCK_TRANSCRIPT)
    parser.add_argument(
        "--streaming", action="store_true", default=False,
        help="Use StreamingPipeline (MT→TTS overlap) and report t_first_byte_ms",
    )
    parser.add_argument(
        "--stt-provider", choices=["prerecorded", "streaming"], default="prerecorded",
        help="prerecorded: Deepgram REST API; streaming: Deepgram WebSocket (simulates live mic)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.mode == "real" and not args.audio_file.exists():
        nearby_audio = sorted(
            path
            for suffix in ("*.wav", "*.mp3", "*.m4a")
            for path in args.audio_file.parent.glob(suffix)
        )
        message = [f"Audio file not found: {args.audio_file}"]
        if nearby_audio:
            message.append("Available audio files near that path:")
            message.extend(f"- {path}" for path in nearby_audio)
        else:
            message.append("Add a fake Spanish test WAV, MP3, or M4A file and try again.")
        raise SystemExit("\n".join(message))

    output = args.output
    if output is None:
        output = Path("runs/stage1-output.txt" if args.mode == "mock" else "runs/stage1-output.mp3")

    latency_log = LatencyLog(args.latency_log)
    if args.streaming:
        pipeline = build_streaming_pipeline(
            mode=args.mode,
            tts_provider=args.tts_provider,
            stt_provider=args.stt_provider,
            latency_log=latency_log,
            mock_transcript=args.mock_transcript,
        )
    else:
        pipeline = build_stage1_pipeline(
            mode=args.mode,
            tts_provider=args.tts_provider,
            latency_log=latency_log,
            mock_transcript=args.mock_transcript,
        )
    try:
        result = pipeline.run(audio_path=args.audio_file, output_path=output)
    except ProviderError as exc:
        raise SystemExit(f"Bridge Stage 1 provider error\n{exc}")

    print("Bridge Stage 1 spike complete")
    print(f"mode:         {args.mode}")
    print(f"streaming:    {args.streaming}")
    if args.mode == "real":
        print(f"tts provider: {args.tts_provider}")
    print(f"transcript:   {result.transcript}")
    print(f"translation:  {result.translation}")
    print(f"output:       {result.output_path}")
    print(f"latency log:  {result.latency_log_path}")
    print(f"total ms:     {result.total_ms}")
    if result.t_first_byte_ms is not None:
        target_met = result.t_first_byte_ms < 2000
        marker = "PASS" if target_met else "FAIL"
        print(f"t_first_byte: {result.t_first_byte_ms} ms  [{marker}] (<2000ms target)")
    if result.sentence_count is not None:
        print(f"sentences:    {result.sentence_count}")
    print("")
    print("stage timings:")
    for timing in latency_log.timings:
        status = "ok" if timing.ok else "failed"
        print(f"- {timing.stage}: {timing.elapsed_ms} ms ({status})")


if __name__ == "__main__":
    main()

