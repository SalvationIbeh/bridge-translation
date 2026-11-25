"""
Unit tests for the periodic deep-liveness re-check (S7). No GPU, no server:

    python server/test_liveness.py

Covers: try_deliver_probe delivery, Metrics.record_liveness threshold + recovery,
synthetic-session exclusion from real metrics, and run_liveness_loop readiness
transitions over a scripted probe sequence.
"""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace as NS

from monitoring.metrics import Metrics
from pipeline.liveness import run_liveness_loop, try_deliver_probe


def test_try_deliver_probe() -> None:
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        probes = {"__liveness__abc": fut}
        # Unknown (real) session → not a probe.
        assert try_deliver_probe(probes, NS(session_id="realsession")) is False
        # Registered probe → handled, future receives the job.
        job = NS(session_id="__liveness__abc", tts_audio=b"x")
        assert try_deliver_probe(probes, job) is True
        assert fut.done() and fut.result() is job
        # Already-resolved future → still reports handled, no crash.
        assert try_deliver_probe(probes, NS(session_id="__liveness__abc")) is True
    finally:
        loop.close()


def test_record_liveness_threshold_and_recovery() -> None:
    m = Metrics()
    m.mark_ready()
    assert m.ready
    # threshold=2: a single failure keeps the node ready.
    assert m.record_liveness(False, None, threshold=2) is True
    assert m.ready and m.consecutive_liveness_failures == 1
    # second consecutive failure → not ready.
    assert m.record_liveness(False, None, threshold=2) is False
    assert not m.ready and m.liveness_failures == 2
    # one success → recovery + streak reset + latency recorded.
    assert m.record_liveness(True, 123.4, threshold=2) is True
    assert m.ready and m.consecutive_liveness_failures == 0
    assert m.last_liveness_ms == 123.4
    assert m.liveness_checks == 3
    # liveness surfaces in the prometheus text.
    prom = m.prometheus()
    assert "linguabridge_liveness_ok 1" in prom
    assert "linguabridge_liveness_failures_total 2" in prom


def test_synthetic_excluded_from_metrics() -> None:
    m = Metrics()
    # Synthetic warm-up / liveness jobs must not move real counters.
    m.record_stt(NS(session_id="__liveness__zz", stt_start=1.0, stt_end=1.1))
    m.record_error("translate", "__warmup_mt__es_en")
    assert m.jobs_processed["stt"] == 0
    assert m.jobs_failed["translate"] == 0
    # A real job does count.
    m.record_stt(NS(session_id="abcd1234", stt_start=1.0, stt_end=1.2))
    m.record_error("translate", "abcd1234")
    assert m.jobs_processed["stt"] == 1
    assert m.jobs_failed["translate"] == 1


async def _run_loop_scripted(seq, threshold=2, interval=0.01, timeout=0.05):
    """Drive run_liveness_loop with a scripted (ok, ms) sequence; return the
    `ready` state observed after each probe."""
    m = Metrics()
    m.mark_ready()
    states: list[bool] = []
    idx = {"i": 0}
    done = asyncio.Event()

    async def probe_once(_t):
        i = idx["i"]
        idx["i"] += 1
        if i >= len(seq):
            done.set()
            await asyncio.sleep(3600)   # park until cancelled
        return seq[i]

    orig = m.record_liveness

    def capturing(ok, ms, th):
        r = orig(ok, ms, th)
        states.append(r)
        return r

    m.record_liveness = capturing  # type: ignore[assignment]

    task = asyncio.create_task(run_liveness_loop(m, probe_once, interval, timeout, threshold))
    await asyncio.wait_for(done.wait(), timeout=5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return states


def test_loop_transitions() -> None:
    # ok → fail → fail (down) → ok (recover)
    seq = [(True, 10.0), (False, None), (False, None), (True, 12.0)]
    states = asyncio.run(_run_loop_scripted(seq, threshold=2))
    assert states == [True, True, False, True], states


def main() -> int:
    test_try_deliver_probe()
    test_record_liveness_threshold_and_recovery()
    test_synthetic_excluded_from_metrics()
    test_loop_transitions()
    print("ALL LIVENESS TESTS PASS")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, ".")
    raise SystemExit(main())
