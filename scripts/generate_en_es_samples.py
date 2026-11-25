"""
generate_en_es_samples.py — synthesize the English source clips for the en→es
reverse-leg benchmark (council decision docs/decisions/2026-06-09).

Reads samples/stage2-en-es.json and writes one 16kHz 16-bit mono raw PCM clip
per case to samples/realtime-pcm-en/<id>.pcm — the same format the es→en clips
use, so make_telephony_ladder.py and benchmark_s1_telephony.py work unchanged:

    python scripts/generate_en_es_samples.py
    python scripts/make_telephony_ladder.py --in samples/realtime-pcm-en --out samples/telephony-pcm-en

Unlike generate_stage1_samples.py (mp3 output), this requests raw PCM straight
from OpenAI TTS (`response_format: "pcm"` → 24kHz s16le mono) and resamples to
16kHz with a numpy FFT — no ffmpeg/pydub dependency on the dev box.

Output clips are gitignored derived audio; this script + the manifest are the
committed artifacts. Cost is a few cents of OpenAI TTS — log it in the ledger:
    python scripts/log_cost.py add --provider openai --usd 0.05 --estimate --note "en->es sample TTS"
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bridge_spike.config import ProviderConfig, load_dotenv  # noqa: E402 — needs the sys.path bootstrap above

OPENAI_PCM_RATE = 24000   # /v1/audio/speech response_format=pcm is 24kHz s16le mono
TARGET_RATE = 16000       # server/benchmark clip format


def resample_24k_to_16k(pcm24: bytes) -> bytes:
    """FFT resample 24kHz s16le → 16kHz s16le (low-pass + 2:3 decimate in one)."""
    x = np.frombuffer(pcm24, dtype="<i2").astype(np.float64) / 32768.0
    n_in = len(x)
    n_out = int(n_in * TARGET_RATE / OPENAI_PCM_RATE)
    spec = np.fft.rfft(x)
    spec_out = spec[: n_out // 2 + 1] * (n_out / n_in)
    y = np.fft.irfft(spec_out, n=n_out)
    return (np.clip(y, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def synthesize_pcm(api_key: str, model: str, voice: str, text: str) -> bytes:
    """One OpenAI TTS call returning raw 24kHz PCM."""
    body = {
        "model": model,
        "voice": voice,
        "input": text,
        "response_format": "pcm",
        "instructions": (
            "Speak clearly and neutrally, like a customer-service agent on a phone "
            "call. Natural pacing, easy to understand."
        ),
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"OpenAI TTS HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"OpenAI TTS request failed: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path, default=Path("samples/stage2-en-es.json"))
    ap.add_argument("--out-dir", type=Path, default=Path("samples/realtime-pcm-en"))
    ap.add_argument("--voice", default=None, help="OpenAI TTS voice (default: OPENAI_TTS_VOICE/coral)")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    load_dotenv()
    config = ProviderConfig.from_env()
    if not config.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is required to generate sample audio.")
    voice = args.voice or config.openai_tts_voice

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    cases = manifest["cases"]
    args.out_dir.mkdir(parents=True, exist_ok=True)

    print(f"en→es sample generation → {args.out_dir}  (model={config.openai_tts_model}, voice={voice})")
    generated = skipped = 0
    for case in cases:
        out = args.out_dir / f"{case['id']}.pcm"
        if out.exists() and not args.overwrite:
            skipped += 1
            print(f"- {case['id']}: kept existing {out}")
            continue
        pcm24 = synthesize_pcm(config.openai_api_key, config.openai_tts_model, voice, case["reference"])
        pcm16 = resample_24k_to_16k(pcm24)
        out.write_bytes(pcm16)
        dur = len(pcm16) / 2 / TARGET_RATE
        print(f"- {case['id']}: wrote {out}  ({dur:.2f}s)")
        generated += 1

    print(f"\ngenerated: {generated}\nskipped:   {skipped}")
    print("next:      python scripts/make_telephony_ladder.py "
          "--in samples/realtime-pcm-en --out samples/telephony-pcm-en")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
