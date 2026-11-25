"""
make_telephony_ladder.py — generate the telephony-codec sample ladder for the
Stage 2 S1 validation re-benchmark (STAGE2_DESIGN §9).

The Stage 1 ~550ms / WER 3.3% numbers were measured on CLEAN audio. CLAUDE.md §8
forbids gaming WER on clean clips only — the gate must test real telephony
degradation. This script turns clean 16kHz PCM source clips into the ladder:

  tier1  clean                  (the source — left as-is, baseline)
  tier2  mu-law "8kHz"          (telephony passband ~300–3400 Hz + G.711 μ-law companding)
  tier3  tier2 + background noise   (informational; target WER ≤ 12%)

Telephony degradation is simulated as band-limiting to the narrowband telephone
passband + 8-bit μ-law companding — the two losses that actually move STT WER.
Output stays 16kHz mono s16le (Whisper resamples internally anyway), so the clips
stream straight into /ws/translate; the degradation is baked in.

NumPy-only — no librosa/audioop, runs on any Python. Outputs are gitignored
derived audio; this *script* is the committed artifact.

Usage:
    python scripts/make_telephony_ladder.py \
        --in samples/realtime-pcm --out samples/telephony-pcm --snr-db 15
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

try:  # Windows consoles default to cp1252 and choke on → / μ / ≤
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SAMPLE_RATE = 16000      # source + output rate (server expects 16kHz mono s16le)
TEL_LOW_HZ = 300.0       # telephony passband low edge
TEL_HIGH_HZ = 3400.0     # telephony passband high edge (≈ 8kHz narrowband)
TEL_ROLL_HZ = 200.0      # raised-cosine transition width (avoids brick-wall ringing)
_MU = 255.0


def mulaw_roundtrip(x: np.ndarray) -> np.ndarray:
    """G.711-style μ-law encode→decode on float audio in [-1, 1].

    Companding + 8-bit quantization is the codec loss on a telephony line;
    this reproduces that quantization distortion (not bit-exact segment coding,
    but the right shape of loss for a WER stress test)."""
    sign = np.sign(x)
    mag = np.log1p(_MU * np.abs(x)) / np.log1p(_MU)        # compress → [-1, 1]
    code = np.round((sign * mag + 1.0) / 2.0 * _MU)        # quantize → 8-bit
    y = code / _MU * 2.0 - 1.0                              # dequantize → [-1, 1]
    return np.sign(y) * (1.0 / _MU) * (np.power(1.0 + _MU, np.abs(y)) - 1.0)


def telephony_band(x: np.ndarray, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Band-pass to the telephone passband (~300–3400 Hz) with smooth raised-cosine
    edges, via FFT. Captures the bandwidth loss of an 8kHz narrowband line."""
    n = len(x)
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1.0 / sr)
    gain = np.ones_like(f)
    lo, hi, roll = TEL_LOW_HZ, TEL_HIGH_HZ, TEL_ROLL_HZ
    gain[f < lo - roll] = 0.0
    m = (f >= lo - roll) & (f < lo)
    gain[m] = 0.5 * (1.0 - np.cos(np.pi * (f[m] - (lo - roll)) / roll))
    gain[f > hi + roll] = 0.0
    m = (f > hi) & (f <= hi + roll)
    gain[m] = 0.5 * (1.0 + np.cos(np.pi * (f[m] - hi) / roll))
    return np.fft.irfft(spec * gain, n=n).astype(np.float32)


def telephony_degrade(x16: np.ndarray) -> np.ndarray:
    """Clean 16kHz float → telephony band-limit → μ-law round-trip."""
    band = telephony_band(x16)
    return mulaw_roundtrip(np.clip(band, -1.0, 1.0))


def add_noise(x: np.ndarray, snr_db: float, rng: np.random.Generator) -> np.ndarray:
    """Add Gaussian background noise at a target SNR (dB)."""
    sig_power = np.mean(x ** 2) + 1e-12
    noise_power = sig_power / (10.0 ** (snr_db / 10.0))
    noise = rng.normal(0.0, np.sqrt(noise_power), size=x.shape).astype(np.float32)
    return x + noise


def _f32_to_pcm16(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def _pcm16_to_f32(raw: bytes) -> np.ndarray:
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="in_dir", default="samples/realtime-pcm")
    ap.add_argument("--out", dest="out_dir", default="samples/telephony-pcm")
    ap.add_argument("--snr-db", type=float, default=15.0, help="tier3 noise SNR")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    in_dir, out_dir = Path(args.in_dir), Path(args.out_dir)
    pcms = sorted(in_dir.glob("*.pcm"))
    if not pcms:
        print(f"ERROR: no .pcm files in {in_dir}", file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(args.seed)

    print(f"Source: {in_dir}  ({len(pcms)} clips)  →  {out_dir}")
    print(f"{'clip':<22}{'dur_s':>8}{'clean_rms':>12}{'tier2_rms':>12}{'tier3_rms':>12}")
    for p in pcms:
        x16 = _pcm16_to_f32(p.read_bytes())
        dur = len(x16) / SAMPLE_RATE

        tier2 = telephony_degrade(x16)
        tier3 = add_noise(tier2, args.snr_db, rng)

        (out_dir / f"{p.stem}_mulaw8k.pcm").write_bytes(_f32_to_pcm16(tier2))
        (out_dir / f"{p.stem}_mulaw8k_noise{int(args.snr_db)}db.pcm").write_bytes(_f32_to_pcm16(tier3))

        print(f"{p.stem:<22}{dur:>8.2f}{np.sqrt(np.mean(x16**2)):>12.4f}"
              f"{np.sqrt(np.mean(tier2**2)):>12.4f}{np.sqrt(np.mean(tier3**2)):>12.4f}")

    print(f"\nDone. tier2 = telephony band + μ-law (target WER ≤ 8%); "
          f"tier3 = +{args.snr_db:.0f}dB noise (target WER ≤ 12%, informational).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
