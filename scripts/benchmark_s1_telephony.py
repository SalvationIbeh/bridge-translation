"""
benchmark_s1_telephony.py — Stage 2 S1 validation gate (STAGE2_DESIGN §9).

Streams the telephony sample ladder at a running Bridge `/ws/translate` server
(on the RunPod persistent T4) and measures, per degradation tier:

  • post-speech latency  — end_of_stream → translated audio arrives (turn-based,
                            the CLAUDE.md §3 metric)
  • WER                  — server STT `original` vs the reference Spanish transcript

This is the gate that decides whether the clean-audio ~550ms / 3.3% WER survives
real telephony. Targets (CLAUDE.md §8 sample ladder):

  tier   audio                       latency gate        WER gate
  clean  16kHz studio                P95 < 2000ms        ≤ 3%   (baseline)
  mulaw8k  μ-law 8kHz codec          P95 < 2000ms        ≤ 8%   ← THE GATE
  noise  μ-law 8kHz + bg noise       (informational)     ≤ 12%

Default direction is es→en. For the en→es reverse leg (council decision
docs/decisions/2026-06-09) point it at the English clip set:
    --src en --tgt es --manifest samples/stage2-en-es.json \
        --clean-dir samples/realtime-pcm-en --ladder-dir samples/telephony-pcm-en

Prereqs:
  1. Server running on the pod:  AUTH_TOKEN=... MODELS_DIR=/models python server/main.py
  2. Ladder generated:           python scripts/make_telephony_ladder.py
  3. Deps here: `websockets` only (no GPU/audio libs — can run locally against the pod URL)

Usage:
    SERVER_URL=wss://<pod>-8000.proxy.runpod.net/ws/translate AUTH_TOKEN=<tok> \
        python scripts/benchmark_s1_telephony.py --runs 3

Output: runs/s1-telephony-<timestamp>.json  + a summary table with PASS/FAIL.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

try:  # Windows consoles default to cp1252 and choke on ✅ / ≤ / →
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import websockets
except ImportError:
    print("ERROR: `websockets` not installed (pip install websockets).", file=sys.stderr)
    raise SystemExit(1)

CHUNK_MS = 20
SAMPLE_RATE = 16000
CHUNK_BYTES = SAMPLE_RATE * CHUNK_MS // 1000 * 2   # 16-bit mono → 640 bytes/20ms

# Per-tier gates (post-speech P95 ms, WER fraction). None = informational only.
TIER_GATES = {
    "clean":   {"p95_ms": 2000, "wer": 0.03},
    "mulaw8k": {"p95_ms": 2000, "wer": 0.08},
    "noise":   {"p95_ms": None, "wer": 0.12},
}


# ── WER ─────────────────────────────────────────────────────────────────────────
def _normalize(s: str) -> list[str]:
    """Lowercase, strip diacritics + punctuation, tokenize. Matches the de-accented
    reference manifest so accent-only diffs don't inflate WER."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return s.split()


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref, hyp = _normalize(reference), _normalize(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    # Levenshtein distance over word tokens
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1] / len(ref)


def pct(values: list[float], p: int) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(int(len(s) * p / 100), len(s) - 1)], 1)


# ── auth ────────────────────────────────────────────────────────────────────────
def make_auth_provider(static_token: str):
    """Signed tokens (S3) are single-use per connection, so when AUTH_SIGNING_KEY
    is set, return a callable that mints a fresh token per connection; otherwise
    the static token string. stream_clip accepts either."""
    key = os.getenv("AUTH_SIGNING_KEY")
    if not key:
        return static_token
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
    from auth import mint_token
    return lambda: mint_token(key, ttl_s=300)


# ── reference transcripts ───────────────────────────────────────────────────────
def load_refs(manifest_path: Path) -> dict[str, str]:
    """`reference` = source-language STT WER target (stage2-en-es.json);
    `expected_spanish` is the legacy key in stage1-benchmark.json."""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {case["id"]: case.get("reference") or case.get("expected_spanish", "")
            for case in data["cases"]}


def base_id(pcm_stem: str) -> str:
    """card_block_mulaw8k_noise15db → card_block (strip ladder suffix)."""
    return pcm_stem.split("_mulaw8k")[0]


# ── one streamed clip ───────────────────────────────────────────────────────────
async def stream_clip(url: str, auth, pcm: bytes, src: str, tgt: str,
                      realtime: bool, timeout: float) -> dict:
    """Stream one clip, return one run's measurements (or {'error': ...}).
    `auth` is a token string, or a callable minting one per connection (S3)."""
    try:
        async with websockets.connect(url, max_size=None, open_timeout=15) as ws:
            await ws.send(json.dumps({"type": "init",
                                      "auth": auth() if callable(auth) else auth,
                                      "source_lang": src, "target_lang": tgt}))
            ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if ready.get("type") != "ready":
                return {"error": f"no ready frame: {ready}"}

            for i in range(0, len(pcm), CHUNK_BYTES):
                await ws.send(pcm[i:i + CHUNK_BYTES])
                if realtime:
                    await asyncio.sleep(CHUNK_MS / 1000)

            # "speaker stopped" — start the turn-based clock here
            t_end_speech = time.perf_counter()
            await ws.send(json.dumps({"type": "end_of_stream"}))

            transcript_ms = original = translated = server_latency = stages = None
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
                if isinstance(msg, str):
                    f = json.loads(msg)
                    if f.get("type") == "transcript":
                        transcript_ms = round((time.perf_counter() - t_end_speech) * 1000, 1)
                        original, translated = f.get("original"), f.get("translated")
                        server_latency, stages = f.get("latency_ms"), f.get("stage_ms")
                    elif f.get("type") == "error":
                        return {"error": f"server: {f}"}
                    # queue_status frames ignored
                elif isinstance(msg, bytes):
                    post_speech_ms = round((time.perf_counter() - t_end_speech) * 1000, 1)
                    net_ms = (round(post_speech_ms - server_latency, 1)
                              if server_latency is not None else None)
                    return {
                        "post_speech_ms": post_speech_ms,   # ← headline turn-based metric
                        "transcript_ms":  transcript_ms,
                        "server_latency_ms": server_latency,
                        "network_overhead_ms": net_ms,
                        "stage_ms": stages,
                        "original": original,
                        "translated": translated,
                        "audio_bytes": len(msg),
                    }
    except asyncio.TimeoutError:
        return {"error": "timeout (likely empty transcript — STT produced nothing)"}
    except Exception as e:  # noqa: BLE001 — benchmark: report, don't crash the run
        return {"error": f"{type(e).__name__}: {e}"}


# ── tier discovery ──────────────────────────────────────────────────────────────
def discover(clean_dir: Path, ladder_dir: Path) -> dict[str, list[Path]]:
    tiers: dict[str, list[Path]] = {
        "clean":   sorted(clean_dir.glob("*.pcm")),
        "mulaw8k": sorted(p for p in ladder_dir.glob("*_mulaw8k.pcm")),
        "noise":   sorted(ladder_dir.glob("*_mulaw8k_noise*db.pcm")),
    }
    return {tier: files for tier, files in tiers.items() if files}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--server", default=os.getenv("SERVER_URL", "ws://localhost:8000/ws/translate"))
    ap.add_argument("--auth", default=os.getenv("AUTH_TOKEN", "test"))
    ap.add_argument("--src", default="es")
    ap.add_argument("--tgt", default="en")
    ap.add_argument("--runs", type=int, default=3, help="runs per clip per tier")
    ap.add_argument("--tiers", default="clean,mulaw8k,noise")
    ap.add_argument("--clean-dir", default="samples/realtime-pcm")
    ap.add_argument("--ladder-dir", default="samples/telephony-pcm")
    ap.add_argument("--manifest", default="samples/stage1-benchmark.json")
    ap.add_argument("--no-realtime", action="store_true", help="stream fast (skip 20ms pacing)")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    auth = make_auth_provider(args.auth)
    refs = load_refs(Path(args.manifest))
    available = discover(Path(args.clean_dir), Path(args.ladder_dir))
    want = [t for t in args.tiers.split(",") if t.strip()]
    tiers = {t: available[t] for t in want if t in available}

    missing = [t for t in want if t not in available]
    if missing:
        print(f"WARN: no clips for tier(s) {missing} — "
              f"run scripts/make_telephony_ladder.py first.", file=sys.stderr)
    if not tiers:
        print("ERROR: no clips to benchmark.", file=sys.stderr)
        return 1

    print(f"S1 telephony benchmark → {args.server}  ({args.src}→{args.tgt}, "
          f"{args.runs} run(s)/clip, realtime={not args.no_realtime})")
    print("=" * 72)

    tier_reports: dict[str, dict] = {}
    for tier, files in tiers.items():
        print(f"\n[{tier}]  {len(files)} clips × {args.runs} runs")
        clip_results = []
        for path in files:
            ref = refs.get(base_id(path.stem), "")
            pcm = path.read_bytes()
            runs = []
            for _ in range(args.runs):
                r = await stream_clip(args.server, auth, pcm, args.src,
                                      args.tgt, not args.no_realtime, args.timeout)
                if "error" not in r:
                    r["wer"] = round(word_error_rate(ref, r["original"]), 4)
                runs.append(r)
            ok = [r for r in runs if "error" not in r]
            wer = (sum(r["wer"] for r in ok) / len(ok)) if ok else None
            lat = sorted(r["post_speech_ms"] for r in ok)
            med = lat[len(lat) // 2] if lat else None
            clip_results.append({"clip": path.stem, "ref": ref, "runs": runs,
                                 "wer_mean": round(wer, 4) if wer is not None else None,
                                 "post_speech_p50": med})
            errs = len(runs) - len(ok)
            if wer is not None:
                print(f"  {path.stem:<34} p50={med:>6}ms  WER={wer * 100:.1f}%")
            else:
                print(f"  {path.stem:<34} ALL FAILED ({errs} errors)")

        all_lat = [r["post_speech_ms"] for c in clip_results for r in c["runs"] if "error" not in r]
        all_wer = [r["wer"] for c in clip_results for r in c["runs"] if "error" not in r]
        gate = TIER_GATES.get(tier, {})
        p50, p95 = pct(all_lat, 50), pct(all_lat, 95)
        wer_mean = round(sum(all_wer) / len(all_wer), 4) if all_wer else None
        lat_pass = (gate.get("p95_ms") is None) or (p95 is not None and p95 < gate["p95_ms"])
        wer_pass = (gate.get("wer") is None) or (wer_mean is not None and wer_mean <= gate["wer"])
        tier_reports[tier] = {
            "clips": clip_results,
            "post_speech_p50_ms": p50, "post_speech_p95_ms": p95,
            "wer_mean": wer_mean, "n_ok": len(all_lat), "n_total": len(clip_results) * args.runs,
            "gate": gate, "lat_pass": lat_pass, "wer_pass": wer_pass,
        }

    # ── summary ──────────────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print(f"{'tier':<10}{'P50':>9}{'P95':>9}{'WER':>9}{'gate P95':>11}{'gate WER':>10}{'verdict':>10}")
    overall_pass = True
    for tier, rep in tier_reports.items():
        g = rep["gate"]
        passed = rep["lat_pass"] and rep["wer_pass"]
        verdict = "PASS" if passed else "FAIL"
        if g.get("p95_ms") is not None and not passed:
            overall_pass = False
        p50_s = f"{rep['post_speech_p50_ms']}ms"
        p95_s = f"{rep['post_speech_p95_ms']}ms"
        wer_s = f"{rep['wer_mean'] * 100:.1f}%" if rep["wer_mean"] is not None else "-"
        gate_p95 = f"<{g['p95_ms']}ms" if g.get("p95_ms") else "info"
        gate_wer = f"<={g['wer'] * 100:.0f}%" if g.get("wer") is not None else "-"
        print(f"{tier:<10}{p50_s:>9}{p95_s:>9}{wer_s:>9}{gate_p95:>11}{gate_wer:>10}{verdict:>10}")

    print("\n" + ("✅ S1 GATE PASS — self-hosted stack holds under telephony."
                  if overall_pass else
                  "❌ S1 GATE FAIL — see per-clip WER/latency; tune before trusting self-host."))

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report = {
        "benchmark": "s1_telephony",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "server": args.server, "direction": f"{args.src}->{args.tgt}",
        "runs_per_clip": args.runs, "realtime": not args.no_realtime,
        "metric": "post_speech_ms = end_of_stream → translated audio (turn-based, CLAUDE.md §3)",
        "wer_note": "normalized: lowercased, diacritics + punctuation stripped",
        "tiers": tier_reports,
        "overall_pass": overall_pass,
    }
    out = Path("runs") / f"s1-telephony-{ts}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReport → {out}")
    return 0 if overall_pass else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
