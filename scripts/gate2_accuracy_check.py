"""
Gate 2 Accuracy Check — Whisper base vs small on the 5 standard CS clips.

Gate 2 rule (quality-guardrails.md): any Whisper model upgrade must pass a
transcription spot-check on samples/realtime-pcm/*.pcm before the new model
is committed to the Dockerfile.

Pass criteria:
  - WER < 10% per clip (generous — these are short, clean synthetic clips)
  - No word substitutions that change meaning (manual eyeball from output)

Usage:
    python scripts/gate2_accuracy_check.py
    python scripts/gate2_accuracy_check.py --models base small
    python scripts/gate2_accuracy_check.py --device cuda:0   # if GPU available

Reads:  samples/realtime-pcm/*.pcm  (16kHz 16-bit mono raw PCM)
        samples/stage1-benchmark.json (expected Spanish reference text)
Writes: runs/gate2-<timestamp>.json
"""
import argparse
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel

# Force UTF-8 on Windows consoles that default to cp1252
import sys
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SAMPLE_RATE = 16_000
PCM_DIR = Path("samples/realtime-pcm")
BENCHMARK_JSON = Path("samples/stage1-benchmark.json")
RUNS_DIR = Path("runs")

# Maps PCM file stem → benchmark case id
PCM_TO_CASE = {
    "card_block": "card_block",
    "balance_check": "balance_check",
    "payment_rejected": "payment_rejected",
    "transfer_pending": "transfer_pending",
    "card_arrival": "card_arrival",
}

WER_PASS_THRESHOLD = 0.10  # 10%


def load_pcm(path: Path) -> np.ndarray:
    """Raw 16-bit mono PCM → float32 in [-1, 1]."""
    raw = path.read_bytes()
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def normalize(text: str) -> str:
    """
    Lowercase, strip accents, strip punctuation, collapse whitespace.
    Accent-stripping prevents artificial WER inflation when the reference
    text uses ASCII approximations (extrano vs extraño).
    """
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def wer(reference: str, hypothesis: str) -> float:
    """Word Error Rate via Levenshtein on word sequences."""
    ref = normalize(reference).split()
    hyp = normalize(hypothesis).split()
    if not ref:
        return 0.0 if not hyp else 1.0

    d = [[0] * (len(hyp) + 1) for _ in range(len(ref) + 1)]
    for i in range(len(ref) + 1):
        d[i][0] = i
    for j in range(len(hyp) + 1):
        d[0][j] = j
    for i in range(1, len(ref) + 1):
        for j in range(1, len(hyp) + 1):
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            d[i][j] = min(
                d[i - 1][j] + 1,
                d[i][j - 1] + 1,
                d[i - 1][j - 1] + cost,
            )
    return d[len(ref)][len(hyp)] / len(ref)


def transcribe_all(model_name: str, device: str, clips: list[dict]) -> list[dict]:
    parts = device.split(":")
    compute = "float16" if parts[0] == "cuda" else "int8"

    print(f"\n  Loading whisper/{model_name} on {device} ({compute})...")
    t0 = time.perf_counter()
    model = WhisperModel(
        model_name,
        device=parts[0],
        device_index=int(parts[1]) if len(parts) > 1 else 0,
        compute_type=compute,
    )
    print(f"  Loaded in {(time.perf_counter() - t0) * 1000:.0f}ms")

    results = []
    for clip in clips:
        audio = load_pcm(clip["pcm_path"])

        t_start = time.perf_counter()
        segments, _ = model.transcribe(
            audio,
            language="es",
            beam_size=5,
            vad_filter=False,
        )
        transcript = " ".join(seg.text.strip() for seg in segments).strip()
        latency_ms = (time.perf_counter() - t_start) * 1000

        error_rate = wer(clip["expected"], transcript)
        passed = error_rate <= WER_PASS_THRESHOLD
        icon = "[PASS]" if passed else "[FAIL]"

        print(
            f"  {icon} {clip['id']:22s}  WER={error_rate:.1%}  {latency_ms:.0f}ms\n"
            f"     ref: {clip['expected']}\n"
            f"     got: {transcript}"
        )

        results.append(
            {
                "clip_id": clip["id"],
                "expected": clip["expected"],
                "transcript": transcript,
                "wer": round(error_rate, 4),
                "latency_ms": round(latency_ms, 1),
                "pass": passed,
            }
        )

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Gate 2 accuracy check")
    parser.add_argument("--models", nargs="+", default=["base", "small"])
    parser.add_argument("--device", default="cpu", help="e.g. cpu or cuda:0")
    args = parser.parse_args()

    manifest = json.loads(BENCHMARK_JSON.read_text())
    case_map = {c["id"]: c for c in manifest["cases"]}

    clips = []
    for pcm_file in sorted(PCM_DIR.glob("*.pcm")):
        case_id = PCM_TO_CASE.get(pcm_file.stem)
        if not case_id or case_id not in case_map:
            print(f"  Warning: no benchmark entry for {pcm_file.name}, skipping.")
            continue
        clips.append(
            {
                "id": case_id,
                "pcm_path": pcm_file,
                "expected": case_map[case_id]["expected_spanish"],
            }
        )

    if not clips:
        print("No PCM clips found. Check samples/realtime-pcm/.")
        return

    print(f"\nGate 2 Accuracy Check — {len(clips)} clips, models: {args.models}")
    print("=" * 70)

    RUNS_DIR.mkdir(exist_ok=True)
    report: dict = {
        "gate": "gate2_accuracy",
        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        "device": args.device,
        "wer_pass_threshold": WER_PASS_THRESHOLD,
        "clip_count": len(clips),
        "models": {},
    }

    for model_name in args.models:
        print(f"\n-- whisper/{model_name} {'-' * (50 - len(model_name))}")
        results = transcribe_all(model_name, args.device, clips)

        avg_wer = sum(r["wer"] for r in results) / len(results)
        avg_lat = sum(r["latency_ms"] for r in results) / len(results)
        gate_pass = all(r["pass"] for r in results)

        print(
            f"\n  avg WER={avg_wer:.1%}  avg latency={avg_lat:.0f}ms  "
            f"Gate2 {'PASS' if gate_pass else 'FAIL'}"
        )

        report["models"][model_name] = {
            "clips": results,
            "avg_wer": round(avg_wer, 4),
            "avg_latency_ms": round(avg_lat, 1),
            "gate2_pass": gate_pass,
        }

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = RUNS_DIR / f"gate2-{ts}.json"
    out_path.write_text(json.dumps(report, indent=2))

    print("\n" + "=" * 70)
    print("GATE 2 VERDICT")
    for model_name, data in report["models"].items():
        print(
            f"  whisper/{model_name:<8}  avg WER={data['avg_wer']:.1%}  "
            f"{'PASS' if data['gate2_pass'] else 'FAIL'}"
        )
    print(f"\nReport → {out_path}")


if __name__ == "__main__":
    main()
