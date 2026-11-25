"""
benchmark_s2_alternating.py — alternating-direction latency test (council
condition, docs/decisions/2026-06-09: "test alternating directions").

Interleaves turns es→en, en→es, es→en, … against a running /ws/translate server,
cycling through both clean clip sets for N rounds. This is what a real bilingual
call does between customer and agent turns — it catches direction-switch costs
(model-cache swaps, voice swaps) that single-direction benchmarks hide.

Reuses stream_clip / WER / percentiles from benchmark_s1_telephony.py — one turn
here is identical to one S1 run, only the direction flips between turns.

Prereqs: both clip sets generated —
    samples/realtime-pcm     (Spanish, existing)
    samples/realtime-pcm-en  (python scripts/generate_en_es_samples.py)

Usage:
    SERVER_URL=wss://<pod>-8000.proxy.runpod.net/ws/translate AUTH_TOKEN=<tok> \
        python scripts/benchmark_s2_alternating.py --rounds 3

Output: runs/s2-alternating-<timestamp>.json + per-direction summary.
Gate (informational, council condition): both directions post-speech P95 < 2000ms.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from itertools import cycle
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark_s1_telephony import (load_refs, make_auth_provider, pct,
                                    stream_clip, word_error_rate)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

P95_GATE_MS = 2000


def clips_for(direction: dict, refs: dict[str, str]) -> list[tuple[Path, str]]:
    paths = sorted(Path(direction["clean_dir"]).glob("*.pcm"))
    return [(p, refs.get(p.stem, "")) for p in paths]


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--server", default=os.getenv("SERVER_URL", "ws://localhost:8000/ws/translate"))
    ap.add_argument("--auth", default=os.getenv("AUTH_TOKEN", "test"))
    ap.add_argument("--rounds", type=int, default=3,
                    help="full passes over the interleaved clip sequence")
    ap.add_argument("--es-dir", default="samples/realtime-pcm")
    ap.add_argument("--en-dir", default="samples/realtime-pcm-en")
    ap.add_argument("--es-manifest", default="samples/stage1-benchmark.json")
    ap.add_argument("--en-manifest", default="samples/stage2-en-es.json")
    ap.add_argument("--no-realtime", action="store_true", help="stream fast (skip 20ms pacing)")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    auth = make_auth_provider(args.auth)
    directions = {
        "es->en": {"src": "es", "tgt": "en", "clean_dir": args.es_dir,
                   "refs": load_refs(Path(args.es_manifest))},
        "en->es": {"src": "en", "tgt": "es", "clean_dir": args.en_dir,
                   "refs": load_refs(Path(args.en_manifest))},
    }
    pools = {}
    for name, d in directions.items():
        clips = clips_for(d, d["refs"])
        if not clips:
            print(f"ERROR: no .pcm clips in {d['clean_dir']} for {name} — "
                  f"generate the clip set first.", file=sys.stderr)
            return 1
        pools[name] = cycle(clips)

    # Interleaved turn sequence: es->en, en->es, es->en, ... one server turn each.
    n_turns_per_round = sum(len(sorted(Path(d["clean_dir"]).glob("*.pcm")))
                            for d in directions.values())
    total_turns = n_turns_per_round * args.rounds
    print(f"Alternating-direction benchmark → {args.server}")
    print(f"{args.rounds} round(s) × {n_turns_per_round} interleaved turns = {total_turns} turns")
    print("=" * 72)

    results: dict[str, list[dict]] = {name: [] for name in directions}
    order = cycle(directions.keys())
    for turn in range(1, total_turns + 1):
        name = next(order)
        d = directions[name]
        path, ref = next(pools[name])
        r = await stream_clip(args.server, auth, path.read_bytes(),
                              d["src"], d["tgt"], not args.no_realtime, args.timeout)
        if "error" in r:
            print(f"  turn {turn:>3} {name}  {path.stem:<28} ERROR: {r['error']}")
        else:
            r["wer"] = round(word_error_rate(ref, r["original"]), 4)
            print(f"  turn {turn:>3} {name}  {path.stem:<28} "
                  f"post_speech={r['post_speech_ms']:>7}ms  WER={r['wer'] * 100:.1f}%")
        r["clip"] = path.stem
        r["turn"] = turn
        results[name].append(r)

    # ── per-direction summary ────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print(f"{'direction':<10}{'turns':>7}{'ok':>5}{'P50':>9}{'P95':>9}{'WER':>9}{'verdict':>10}")
    summary: dict[str, dict] = {}
    overall_pass = True
    for name, runs in results.items():
        ok = [r for r in runs if "error" not in r]
        lat = [r["post_speech_ms"] for r in ok]
        wers = [r["wer"] for r in ok]
        p50, p95 = pct(lat, 50), pct(lat, 95)
        wer_mean = round(sum(wers) / len(wers), 4) if wers else None
        passed = bool(ok) and p95 is not None and p95 < P95_GATE_MS
        overall_pass = overall_pass and passed
        summary[name] = {"runs": runs, "n_ok": len(ok), "n_total": len(runs),
                         "post_speech_p50_ms": p50, "post_speech_p95_ms": p95,
                         "wer_mean": wer_mean, "p95_gate_ms": P95_GATE_MS, "pass": passed}
        wer_s = f"{wer_mean * 100:.1f}%" if wer_mean is not None else "-"
        p50_s = f"{p50}ms" if p50 is not None else "-"
        p95_s = f"{p95}ms" if p95 is not None else "-"
        print(f"{name:<10}{len(runs):>7}{len(ok):>5}{p50_s:>9}{p95_s:>9}"
              f"{wer_s:>9}{'PASS' if passed else 'FAIL':>10}")

    print("\n" + ("✅ ALTERNATING PASS — both directions hold P95 < "
                  f"{P95_GATE_MS}ms under direction switching."
                  if overall_pass else
                  "❌ ALTERNATING FAIL — check for direction-switch cost "
                  "(model-cache or voice swap) in the per-turn timings."))

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report = {
        "benchmark": "s2_alternating",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "server": args.server,
        "rounds": args.rounds,
        "realtime": not args.no_realtime,
        "metric": "post_speech_ms per turn, directions interleaved es->en / en->es",
        "directions": summary,
        "overall_pass": overall_pass,
    }
    out = Path("runs") / f"s2-alternating-{ts}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReport → {out}")
    return 0 if overall_pass else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
