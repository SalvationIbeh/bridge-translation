from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bridge_spike.factory import build_stage1_pipeline, build_streaming_pipeline
from bridge_spike.latency import LatencyLog
from bridge_spike.providers import ProviderError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark Bridge Stage 1 audio samples.")
    parser.add_argument("--mode", choices=["mock", "real"], default="mock")
    parser.add_argument("--tts-provider", choices=["openai", "elevenlabs", "kokoro"], default="openai")
    parser.add_argument("--manifest", type=Path, default=Path("samples/stage1-benchmark.json"))
    parser.add_argument("--run-dir", type=Path, default=Path("runs/stage1-benchmark"))
    parser.add_argument("--report", type=Path, default=Path("runs/stage1-benchmark-report.json"))
    parser.add_argument(
        "--streaming", action="store_true", default=False,
        help="Use StreamingPipeline (MT→TTS overlap); reports t_first_byte_ms per case",
    )
    parser.add_argument(
        "--stt-provider", choices=["prerecorded", "streaming"], default="prerecorded",
        help="prerecorded: Deepgram REST API; streaming: Deepgram WebSocket (simulates live mic)",
    )
    return parser.parse_args()


def load_manifest(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"Benchmark manifest not found: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Benchmark manifest is invalid JSON: {path}\n{exc}")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise SystemExit(f"Benchmark manifest has no cases: {path}")
    return manifest


def case_paths(case: dict, manifest_path: Path, run_dir: Path, mode: str) -> tuple[Path, Path, Path]:
    case_id = case["id"]
    audio_path = manifest_path.parent / case["audio_file"]
    output_suffix = "txt" if mode == "mock" else "mp3"
    output_path = run_dir / case_id / f"translation.{output_suffix}"
    latency_path = run_dir / case_id / "latency.jsonl"
    return audio_path, output_path, latency_path


def run_case(case: dict, args: argparse.Namespace) -> dict:
    audio_path, output_path, latency_path = case_paths(case, args.manifest, args.run_dir, args.mode)
    result = {
        "id": case["id"],
        "group": case.get("group", "short"),
        "audio_file": str(audio_path),
        "expected_spanish": case.get("expected_spanish", ""),
        "status": "pending",
    }

    if args.mode == "real" and not audio_path.exists():
        result["status"] = "skipped_missing_audio"
        return result

    latency_log = LatencyLog(latency_path)
    if args.streaming:
        pipeline = build_streaming_pipeline(
            mode=args.mode,
            tts_provider=args.tts_provider,
            stt_provider=args.stt_provider,
            latency_log=latency_log,
            mock_transcript=case.get("expected_spanish", ""),
        )
    else:
        pipeline = build_stage1_pipeline(
            mode=args.mode,
            tts_provider=args.tts_provider,
            latency_log=latency_log,
            mock_transcript=case.get("expected_spanish", ""),
        )
    try:
        pipeline_result = pipeline.run(audio_path=audio_path, output_path=output_path)
    except ProviderError as exc:
        result["status"] = "provider_error"
        result["error"] = str(exc)
    else:
        completed: dict = {
            "status": "completed",
            "transcript": pipeline_result.transcript,
            "translation": pipeline_result.translation,
            "output_path": str(pipeline_result.output_path),
            "latency_log_path": str(pipeline_result.latency_log_path),
            "total_ms": pipeline_result.total_ms,
        }
        if pipeline_result.t_first_byte_ms is not None:
            completed["t_first_byte_ms"] = pipeline_result.t_first_byte_ms
        if pipeline_result.sentence_count is not None:
            completed["sentence_count"] = pipeline_result.sentence_count
        result.update(completed)

    result["stage_timings"] = [asdict(timing) for timing in latency_log.timings]
    return result


def summarize(cases: list[dict]) -> dict:
    grouped: dict[str, list[dict]] = {}
    for case in cases:
        grouped.setdefault(case["group"], []).append(case)

    summaries = {}
    for group, rows in grouped.items():
        completed = [row for row in rows if row["status"] == "completed"]
        totals = [row["total_ms"] for row in completed]
        first_bytes = [row["t_first_byte_ms"] for row in completed if "t_first_byte_ms" in row]
        summaries[group] = {
            "cases": len(rows),
            "completed": len(completed),
            "skipped_missing_audio": sum(row["status"] == "skipped_missing_audio" for row in rows),
            "provider_errors": sum(row["status"] == "provider_error" for row in rows),
            "p50_total_ms": round(statistics.median(totals), 2) if totals else None,
            "min_total_ms": round(min(totals), 2) if totals else None,
            "max_total_ms": round(max(totals), 2) if totals else None,
            "p50_t_first_byte_ms": round(statistics.median(first_bytes), 2) if first_bytes else None,
            "min_t_first_byte_ms": round(min(first_bytes), 2) if first_bytes else None,
            "max_t_first_byte_ms": round(max(first_bytes), 2) if first_bytes else None,
        }
    return summaries


def write_report(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def print_report(report: dict) -> None:
    print("Bridge Stage 1 benchmark complete")
    print(f"mode:         {report['mode']}")
    if report["mode"] == "real":
        print(f"tts provider: {report['tts_provider']}")
    print(f"report:       {report['report_path']}")
    print("")
    print("cases:")
    for case in report["cases"]:
        total = f" - {case['total_ms']} ms" if case["status"] == "completed" else ""
        print(f"- {case['id']} [{case['group']}]: {case['status']}{total}")
    print("")
    print("group summary:")
    for group, summary in report["summary"].items():
        p50 = summary["p50_total_ms"]
        p50_text = f"{p50} ms" if p50 is not None else "n/a"
        line = (
            f"- {group}: {summary['completed']}/{summary['cases']} completed, "
            f"p50 total {p50_text}, {summary['skipped_missing_audio']} missing audio"
        )
        p50_fb = summary.get("p50_t_first_byte_ms")
        if p50_fb is not None:
            target_met = p50_fb < 2000
            marker = "[PASS]" if target_met else "[FAIL]"
            line += f", p50 first-byte {p50_fb} ms {marker}"
        print(line)


def main() -> None:
    args = parse_args()
    manifest = load_manifest(args.manifest)
    cases = [run_case(case, args) for case in manifest["cases"]]
    report = {
        "benchmark": manifest.get("name", "stage1"),
        "mode": args.mode,
        "streaming": args.streaming,
        "tts_provider": args.tts_provider,
        "manifest_path": str(args.manifest),
        "report_path": str(args.report),
        "cases": cases,
        "summary": summarize(cases),
    }
    write_report(args.report, report)
    print_report(report)


if __name__ == "__main__":
    main()

