from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bridge_spike.config import ProviderConfig, load_dotenv
from bridge_spike.providers import OpenAITextToSpeech, ProviderError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate synthetic Stage 1 Spanish benchmark audio.")
    parser.add_argument("--manifest", type=Path, default=Path("samples/stage1-benchmark.json"))
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def load_cases(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"Benchmark manifest not found: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Benchmark manifest is invalid JSON: {path}\n{exc}")
    cases = manifest.get("cases")
    if not isinstance(cases, list):
        raise SystemExit(f"Benchmark manifest has no case list: {path}")
    return [case for case in cases if case.get("group") == "short"]


def build_tts() -> OpenAITextToSpeech:
    load_dotenv()
    config = ProviderConfig.from_env()
    if not config.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is required to generate synthetic sample audio.")
    return OpenAITextToSpeech(config)


def main() -> None:
    args = parse_args()
    cases = load_cases(args.manifest)
    tts = build_tts()
    generated = 0
    skipped = 0

    print("Bridge Stage 1 sample generation")
    for case in cases:
        output_path = args.manifest.parent / case["audio_file"]
        if output_path.exists() and not args.overwrite:
            skipped += 1
            print(f"- {case['id']}: kept existing {output_path}")
            continue
        try:
            tts.synthesize(
                case["expected_spanish"],
                output_path=output_path,
                language_code="es",
            )
        except ProviderError as exc:
            raise SystemExit(f"Sample generation stopped at {case['id']}\n{exc}")
        generated += 1
        print(f"- {case['id']}: wrote {output_path}")

    print("")
    print(f"generated: {generated}")
    print(f"skipped:   {skipped}")
    print("next:      python .\\scripts\\benchmark_stage1.py --mode real")


if __name__ == "__main__":
    main()

