"""
s2_multiproc_driver.py — multi-process GPU packing load test (next-pod-session.md #4).

Runs co-located on the pod. Opens --concurrency realtime streams spread round-robin
across --ports (one server process per port, all sharing the GPU), each looping the
/tmp/clips set for --duration seconds. Reports server-side turn latency (P50/P95) and
samples GPU util/VRAM — to test whether the ~4-session/process ceiling is the Python
GIL (scales with processes) or the GPU (does not).

Run on the pod:
  python s2_multiproc_driver.py --ports 8000,8001,8002 --concurrency 12 --duration 45
"""
import argparse
import os
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")
from benchmark_s1_telephony import stream_clip, load_refs  # noqa: E402


def pct(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    i = min(len(s) - 1, int(round((p / 100) * (len(s) - 1))))
    return round(s[i], 1)


async def stream_worker(wid, port, clips, auth, src, tgt, deadline, out):
    url = f"ws://localhost:{port}/ws/translate"
    n = 0
    while time.perf_counter() < deadline:
        pcm, _ref = clips[n % len(clips)]
        r = await stream_clip(url, auth, pcm, src, tgt, realtime=True, timeout=45.0)
        r["port"] = port
        out.append(r)
        n += 1


def sample_gpu(samples, stop):
    while not stop[0]:
        try:
            o = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=4).stdout.strip()
            u, m = o.split(",")
            samples.append((int(u), int(m)))
        except Exception:
            pass
        time.sleep(1.0)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ports", default="8000")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--duration", type=float, default=40.0)
    ap.add_argument("--clip-dir", default="/tmp/clips")
    ap.add_argument("--manifest", default="/tmp/clips/manifest.json")
    ap.add_argument("--auth", default=os.environ.get("AUTH_TOKEN", ""))
    ap.add_argument("--src", default="es")
    ap.add_argument("--tgt", default="en")
    args = ap.parse_args()

    ports = [p.strip() for p in args.ports.split(",") if p.strip()]
    refs = load_refs(Path(args.manifest)) if Path(args.manifest).exists() else {}
    clips = [(p.read_bytes(), refs.get(p.stem, ""))
             for p in sorted(Path(args.clip_dir).glob("*.pcm"))]
    if not clips:
        print("ERROR: no clips", file=sys.stderr)
        return 1

    import threading
    gpu_samples, stop = [], [False]
    threading.Thread(target=sample_gpu, args=(gpu_samples, stop), daemon=True).start()

    out: list = []
    deadline = time.perf_counter() + args.duration
    tasks = [stream_worker(i, ports[i % len(ports)], clips, args.auth,
                           args.src, args.tgt, deadline, out)
             for i in range(args.concurrency)]
    t0 = time.perf_counter()
    await asyncio.gather(*tasks)
    elapsed = time.perf_counter() - t0
    stop[0] = True

    ok = [r for r in out if "error" not in r]
    errs = [r for r in out if "error" in r]
    server_lat = [r["server_latency_ms"] for r in ok if r.get("server_latency_ms") is not None]
    post_lat = [r["post_speech_ms"] for r in ok if r.get("post_speech_ms") is not None]
    by_port = {p: sum(1 for r in ok if str(r.get("port")) == p) for p in ports}
    utils = [u for u, _ in gpu_samples]
    mems = [m for _, m in gpu_samples]

    summary = {
        "ports": ports, "processes": len(ports),
        "concurrency": args.concurrency, "duration_s": round(elapsed, 1),
        "turns_ok": len(ok), "turns_err": len(errs),
        "throughput_turns_per_s": round(len(ok) / elapsed, 2) if elapsed else None,
        "server_latency_ms": {"p50": pct(server_lat, 50), "p95": pct(server_lat, 95),
                              "max": round(max(server_lat), 1) if server_lat else None},
        "post_speech_ms": {"p50": pct(post_lat, 50), "p95": pct(post_lat, 95)},
        "turns_by_port": by_port,
        "gpu_util_pct": {"peak": max(utils) if utils else None,
                         "avg": round(sum(utils) / len(utils), 1) if utils else None},
        "gpu_mem_used_mib": {"peak": max(mems) if mems else None},
        "sample_errors": [e["error"] for e in errs[:3]],
    }
    print("S2MP_RESULT:" + json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
