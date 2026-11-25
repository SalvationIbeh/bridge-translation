"""
benchmark_s2_load.py — S2 concurrent-stream load test (STAGE2_DESIGN §9 S2).

MAX_SESSIONS in the image is aspirational; this measures what the GPU actually
sustains. Ramps concurrency level by level (default 1→2→4→6→8 streams). Each
stream behaves like a live call: it streams the clean clip set sequentially in
real time (20ms pacing), one WS connection per turn. With --directions mixed
(default when the English set exists), odd streams run en→es so the load is
honestly bidirectional.

Per level: post-speech P50/P95, mean WER, error count. A level PASSES if
P95 < 2000ms with zero errors. The recommendation is the highest passing level
— set MAX_SESSIONS to that (capacity claims need the margin note in the report).

Usage:
    SERVER_URL=wss://<pod>-8000.proxy.runpod.net/ws/translate AUTH_TOKEN=<minted> \
        python scripts/benchmark_s2_load.py --levels 1,2,4,6,8

NOTE (S3 auth): each turn opens a fresh WS and signed tokens are single-use
(nonce). Set AUTH_SIGNING_KEY in the environment and the harness mints a fresh
token per connection automatically (same for the S1 and alternating harnesses):
    AUTH_SIGNING_KEY=<key> python scripts/benchmark_s2_load.py

Output: runs/s2-load-<timestamp>.json + per-level summary table.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark_s1_telephony import (load_refs, make_auth_provider, pct,  # noqa: E402
                                    stream_clip, word_error_rate)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

P95_GATE_MS = 2000


async def run_stream(args, auth, direction: dict, results: list[dict]) -> None:
    """One simulated call: stream every clip in the set sequentially, in real time."""
    for path, ref in direction["clips"]:
        r = await stream_clip(args.server, auth, path.read_bytes(),
                              direction["src"], direction["tgt"],
                              not args.no_realtime, args.timeout)
        if "error" not in r:
            r["wer"] = round(word_error_rate(ref, r["original"]), 4)
        r["clip"] = path.stem
        r["direction"] = f"{direction['src']}->{direction['tgt']}"
        results.append(r)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--server", default=os.getenv("SERVER_URL", "ws://localhost:8000/ws/translate"))
    ap.add_argument("--auth", default=os.getenv("AUTH_TOKEN", "test"),
                    help="static token; ignored when AUTH_SIGNING_KEY is set "
                         "(then a fresh signed token is minted per connection)")
    ap.add_argument("--levels", default="1,2,4,6,8", help="concurrency ramp")
    ap.add_argument("--directions", choices=["es-en", "mixed"], default=None,
                    help="default: mixed if the English clip set exists")
    ap.add_argument("--es-dir", default="samples/realtime-pcm")
    ap.add_argument("--en-dir", default="samples/realtime-pcm-en")
    ap.add_argument("--es-manifest", default="samples/stage1-benchmark.json")
    ap.add_argument("--en-manifest", default="samples/stage2-en-es.json")
    ap.add_argument("--no-realtime", action="store_true", help="stream fast (skip 20ms pacing)")
    ap.add_argument("--timeout", type=float, default=45.0)
    args = ap.parse_args()

    auth = make_auth_provider(args.auth)
    auth_mode = "signed" if callable(auth) else "static"

    refs_es = load_refs(Path(args.es_manifest))
    es_clips = [(p, refs_es.get(p.stem, "")) for p in sorted(Path(args.es_dir).glob("*.pcm"))]
    if not es_clips:
        print(f"ERROR: no clips in {args.es_dir}", file=sys.stderr)
        return 1
    en_clips: list = []
    if Path(args.en_dir).exists():
        refs_en = load_refs(Path(args.en_manifest))
        en_clips = [(p, refs_en.get(p.stem, "")) for p in sorted(Path(args.en_dir).glob("*.pcm"))]
    mixed = (args.directions or ("mixed" if en_clips else "es-en")) == "mixed" and bool(en_clips)

    dir_es = {"src": "es", "tgt": "en", "clips": es_clips}
    dir_en = {"src": "en", "tgt": "es", "clips": en_clips}
    levels = [int(x) for x in args.levels.split(",") if x.strip()]

    print(f"S2 load test → {args.server}")
    print(f"levels={levels}  directions={'mixed es↔en' if mixed else 'es→en only'}  "
          f"auth={auth_mode}  realtime={not args.no_realtime}")
    print("=" * 72)

    level_reports: dict[str, dict] = {}
    for n in levels:
        print(f"\n[{n} concurrent stream(s)]")
        results: list[dict] = []
        streams = [run_stream(args, auth, dir_en if (mixed and i % 2) else dir_es, results)
                   for i in range(n)]
        t0 = asyncio.get_event_loop().time()
        await asyncio.gather(*streams)
        wall_s = asyncio.get_event_loop().time() - t0

        ok = [r for r in results if "error" not in r]
        errs = [r for r in results if "error" in r]
        lat = [r["post_speech_ms"] for r in ok]
        wers = [r["wer"] for r in ok]
        p50, p95 = pct(lat, 50), pct(lat, 95)
        wer_mean = round(sum(wers) / len(wers), 4) if wers else None
        passed = not errs and p95 is not None and p95 < P95_GATE_MS
        level_reports[str(n)] = {
            "streams": n, "turns": len(results), "ok": len(ok), "errors": len(errs),
            "error_details": [r["error"] for r in errs],
            "post_speech_p50_ms": p50, "post_speech_p95_ms": p95,
            "wer_mean": wer_mean, "wall_s": round(wall_s, 1), "pass": passed,
            "runs": results,
        }
        wer_s = f"{wer_mean * 100:.1f}%" if wer_mean is not None else "-"
        print(f"  turns={len(results)}  ok={len(ok)}  errors={len(errs)}  "
              f"P50={p50}ms  P95={p95}ms  WER={wer_s}  → {'PASS' if passed else 'FAIL'}")
        for e in {r['error'] for r in errs}:
            print(f"    error: {e}")

    # ── summary + recommendation ────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print(f"{'streams':>8}{'turns':>7}{'errors':>8}{'P50':>9}{'P95':>9}{'WER':>8}{'verdict':>9}")
    recommended = 0
    for n in levels:
        rep = level_reports[str(n)]
        wer_s = f"{rep['wer_mean'] * 100:.1f}%" if rep["wer_mean"] is not None else "-"
        print(f"{n:>8}{rep['turns']:>7}{rep['errors']:>8}{rep['post_speech_p50_ms']!s:>7}ms"
              f"{rep['post_speech_p95_ms']!s:>7}ms{wer_s:>8}{'PASS' if rep['pass'] else 'FAIL':>9}")
        if rep["pass"]:
            recommended = max(recommended, n)

    print(f"\nRecommended MAX_SESSIONS = {recommended} "
          f"(highest level with zero errors and P95 < {P95_GATE_MS}ms)"
          if recommended else
          "\nNo level passed — investigate before any capacity claim.")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report = {
        "benchmark": "s2_load",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "server": args.server,
        "directions": "mixed" if mixed else "es-en",
        "auth_mode": auth_mode,
        "metric": "post_speech_ms per turn under N concurrent realtime streams",
        "p95_gate_ms": P95_GATE_MS,
        "levels": level_reports,
        "recommended_max_sessions": recommended,
    }
    out = Path("runs") / f"s2-load-{ts}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReport → {out}")
    return 0 if recommended else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
