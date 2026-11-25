"""
GPU Latency Benchmark — Whisper small end-to-end on RunPod.

Streams each PCM clip through the live server, records per-stage latency
from the transcript frame, computes P50/P90, writes a JSON run record.

Gate 1 requirement: 5 warm runs per clip, first run discarded as cold-start.
Target: end-to-end P50 < 1000ms (Whisper small may be slower than base's 827ms).

Usage:
    SERVER_URL=wss://<pod>.proxy.runpod.net/ws/translate \\
    AUTH_TOKEN=<token> \\
    python scripts/gpu_latency_benchmark.py

    # or pass directly:
    python scripts/gpu_latency_benchmark.py \\
        --server wss://<pod>.proxy.runpod.net/ws/translate \\
        --token <token> \\
        --runs 6          # 6 total; first discarded
"""
import argparse
import asyncio
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import websockets

PCM_DIR  = Path("samples/realtime-pcm")
RUNS_DIR = Path("runs")

SAMPLE_RATE = 16_000
CHUNK_MS    = 20
CHUNK_BYTES = SAMPLE_RATE * CHUNK_MS // 1000 * 2   # 640 bytes per 20ms chunk


async def run_once(server_url: str, token: str, pcm_path: Path) -> dict:
    """Stream one clip, return latency data from transcript frame."""
    pcm = pcm_path.read_bytes()
    chunks = [pcm[i:i + CHUNK_BYTES] for i in range(0, len(pcm), CHUNK_BYTES)]

    async with websockets.connect(server_url, max_size=10 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "type":        "init",
            "auth":        token,
            "source_lang": "es",
            "target_lang": "en",
        }))
        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready", f"unexpected init response: {ready}"

        t_stream_start = time.perf_counter()
        for chunk in chunks:
            await ws.send(chunk)
            await asyncio.sleep(CHUNK_MS / 1000)

        await ws.send(json.dumps({"type": "end_of_stream"}))
        t_after_stream = time.perf_counter()

        result = {"end_to_end_ms": None, "stage_ms": {}, "transcript": None, "translated": None}

        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=60.0)
            if isinstance(msg, str):
                frame = json.loads(msg)
                if frame.get("type") == "transcript":
                    result["end_to_end_ms"] = frame.get("latency_ms")
                    result["stage_ms"]      = frame.get("stage_ms", {})
                    result["transcript"]    = frame.get("original")
                    result["translated"]    = frame.get("translated")
            elif isinstance(msg, bytes):
                # Audio received — full round trip complete
                t_done = time.perf_counter()
                result["wall_ms_post_stream"] = round((t_done - t_after_stream) * 1000, 1)
                return result


async def benchmark_clip(
    server_url: str, token: str, pcm_path: Path, n_runs: int
) -> dict:
    clip_id = pcm_path.stem
    print(f"\n  {clip_id} ({n_runs} runs, first discarded as cold-start)")
    timings = []

    for i in range(n_runs):
        try:
            result = await run_once(server_url, token, pcm_path)
            ms = result["end_to_end_ms"]
            stage = result["stage_ms"]
            label = "(cold)" if i == 0 else f"run {i}"
            print(
                f"    {label:8s}  e2e={ms}ms  "
                f"stt={stage.get('stt')}ms  "
                f"mt={stage.get('translate')}ms  "
                f"tts={stage.get('tts')}ms"
            )
            if i > 0:   # discard cold-start
                timings.append(result)
        except Exception as e:
            print(f"    run {i} ERROR: {e}")

    if not timings:
        return {"clip_id": clip_id, "error": "all runs failed"}

    e2e_values = [t["end_to_end_ms"] for t in timings if t["end_to_end_ms"] is not None]
    e2e_values.sort()

    def percentile(data, p):
        idx = max(0, int(len(data) * p / 100) - 1)
        return data[idx]

    return {
        "clip_id":   clip_id,
        "n_warm":    len(timings),
        "e2e_p50":   percentile(e2e_values, 50),
        "e2e_p90":   percentile(e2e_values, 90),
        "e2e_mean":  round(statistics.mean(e2e_values), 1),
        "e2e_min":   min(e2e_values),
        "e2e_max":   max(e2e_values),
        "raw_runs":  timings,
    }


async def main(server_url: str, token: str, n_runs: int) -> None:
    pcm_files = sorted(PCM_DIR.glob("*.pcm"))
    if not pcm_files:
        print("No PCM files found in samples/realtime-pcm/")
        return

    print(f"\nGPU Latency Benchmark — whisper/small — {len(pcm_files)} clips x {n_runs} runs")
    print(f"Server: {server_url}")
    print("=" * 70)

    clip_results = []
    for pcm_path in pcm_files:
        clip_results.append(await benchmark_clip(server_url, token, pcm_path, n_runs))

    # Aggregate across all clips (warm runs only)
    all_e2e = [
        r["e2e_p50"] for r in clip_results if "e2e_p50" in r
    ]
    overall_p50 = sorted(all_e2e)[len(all_e2e) // 2] if all_e2e else None
    overall_p90_idx = min(int(len(all_e2e) * 0.9), len(all_e2e) - 1)
    overall_p90 = sorted(all_e2e)[overall_p90_idx] if all_e2e else None

    print("\n" + "=" * 70)
    print("RESULTS SUMMARY")
    print(f"{'Clip':<24}  P50    P90    mean")
    for r in clip_results:
        if "error" in r:
            print(f"  {r['clip_id']:<22}  ERROR")
        else:
            print(
                f"  {r['clip_id']:<22}  {r['e2e_p50']}ms  "
                f"{r['e2e_p90']}ms  {r['e2e_mean']}ms"
            )

    target_ms = 1000
    verdict = "PASS" if overall_p50 and overall_p50 < target_ms else "FAIL"
    print(f"\nOverall P50: {overall_p50}ms  P90: {overall_p90}ms  [{verdict} — target <{target_ms}ms]")

    report = {
        "benchmark": "gpu_latency_small",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "server":    server_url,
        "whisper_model": "small",
        "n_runs_per_clip": n_runs,
        "warm_runs_per_clip": n_runs - 1,
        "overall_p50_ms": overall_p50,
        "overall_p90_ms": overall_p90,
        "gate1_target_ms": target_ms,
        "gate1_pass": verdict == "PASS",
        "clips": clip_results,
    }

    RUNS_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = RUNS_DIR / f"gpu-latency-small-{ts}.json"
    out_path.write_text(json.dumps(report, indent=2))
    print(f"\nReport → {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default=os.getenv("SERVER_URL", ""))
    parser.add_argument("--token",  default=os.getenv("AUTH_TOKEN", ""))
    parser.add_argument("--runs",   type=int, default=6, help="total runs per clip (first discarded)")
    args = parser.parse_args()

    if not args.server:
        parser.error("--server or SERVER_URL env var required")
    if not args.token:
        parser.error("--token or AUTH_TOKEN env var required")

    asyncio.run(main(args.server, args.token, args.runs))
