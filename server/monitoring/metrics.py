"""
Metrics — lightweight in-process metrics.
Exposes Prometheus-compatible /metrics endpoint.
No external dependencies — just a rolling window of latency samples.
"""
import collections
import time
from datetime import datetime, timezone
from threading import Lock

import config


class Metrics:
    def __init__(self, window: int = 500):
        self._lock   = Lock()
        self._window = window

        # Rolling latency samples (milliseconds)
        self.stt_latencies       = collections.deque(maxlen=window)
        self.translate_latencies = collections.deque(maxlen=window)
        self.tts_latencies       = collections.deque(maxlen=window)
        self.total_latencies     = collections.deque(maxlen=window)

        # Counters
        self.jobs_processed  = {"stt": 0, "translate": 0, "tts": 0}
        self.jobs_failed     = {"stt": 0, "translate": 0, "tts": 0}
        self.active_sessions = 0
        self._start_time     = time.monotonic()

        # ── Warmth / readiness (§4.4 reliability path) ───────────────────────
        # A process boot is a cold start by definition; `ready` flips True only
        # after the boot self-test fires every GPU model successfully. An external
        # monitor reads `ready` + `boot_timestamp` to assert "zero cold-starts in
        # 30 days" — the uptime artifact that closes a future customer.
        self.ready             = False
        self._boot_epoch       = time.time()      # wall-clock process start (epoch s)
        self.warm_since_epoch  = None             # epoch s when `ready` became True
        self.boot_duration_ms  = None             # process start → ready
        self.warmup_ms         = None             # best-effort end-to-end warm latency
        self.warmup_stage_ms   = {}               # {stt, translate, tts} warm timings
        self.model_load_ms     = {}               # {vad, mt_es_en, ...} load durations
        self.warmup_failures   = 0                # boot self-test failures this process

        # ── Periodic deep-liveness (§4.4 / S7) ───────────────────────────────
        self.liveness_checks               = 0    # probes fired this process
        self.liveness_failures             = 0    # probes that failed/timed out
        self.consecutive_liveness_failures = 0    # current failure streak
        self.last_liveness_check_epoch     = None # epoch s of the last probe
        self.last_liveness_ok_epoch        = None # epoch s of the last SUCCESS
        self.last_liveness_ms              = None # latency of the last successful probe

    def record_stt(self, job):
        if config.is_synthetic(job.session_id):
            return
        with self._lock:
            self.stt_latencies.append((job.stt_end - job.stt_start) * 1000)
            self.jobs_processed["stt"] += 1

    def record_translate(self, job):
        if config.is_synthetic(job.session_id):
            return
        with self._lock:
            self.translate_latencies.append((job.translate_end - job.translate_start) * 1000)
            self.jobs_processed["translate"] += 1

    def record_tts(self, job):
        if config.is_synthetic(job.session_id):
            return
        with self._lock:
            self.tts_latencies.append((job.tts_end - job.tts_start) * 1000)
            self.jobs_processed["tts"] += 1
            if job.total_ms:
                self.total_latencies.append(job.total_ms)

    def record_error(self, stage: str, session_id: str | None = None):
        if session_id and config.is_synthetic(session_id):
            return
        with self._lock:
            self.jobs_failed[stage] = self.jobs_failed.get(stage, 0) + 1

    def session_opened(self):
        with self._lock:
            self.active_sessions += 1

    def session_closed(self):
        with self._lock:
            self.active_sessions = max(0, self.active_sessions - 1)

    # ── Warmth / readiness ────────────────────────────────────────────────────
    def record_model_load(self, name: str, ms: float):
        with self._lock:
            self.model_load_ms[name] = round(ms, 1)

    def mark_ready(self, warmup_ms: float | None = None, stage_ms: dict | None = None):
        with self._lock:
            self.ready            = True
            self.warm_since_epoch = time.time()
            self.boot_duration_ms = round((time.time() - self._boot_epoch) * 1000, 1)
            if warmup_ms is not None:
                self.warmup_ms = round(warmup_ms, 1)
            if stage_ms:
                self.warmup_stage_ms = {
                    k: round(v, 1) for k, v in stage_ms.items() if v is not None
                }

    def mark_warmup_failed(self):
        with self._lock:
            self.ready = False
            self.warmup_failures += 1

    def record_liveness(self, ok: bool, ms: float | None, threshold: int) -> bool:
        """Record one periodic deep-liveness probe result and update `ready`.
        Flips ready False after `threshold` CONSECUTIVE failures; a single success
        clears the streak and restores ready (GPU recovered). Returns the resulting
        `ready` state. Does NOT touch warm_since (that stays the original boot warm
        time); liveness_failures_total is the separate flap signal."""
        with self._lock:
            self.liveness_checks += 1
            self.last_liveness_check_epoch = time.time()
            if ok:
                self.consecutive_liveness_failures = 0
                self.last_liveness_ok_epoch = time.time()
                if ms is not None:
                    self.last_liveness_ms = round(ms, 1)
                if not self.ready:
                    self.ready = True   # recovery
            else:
                self.liveness_failures += 1
                self.consecutive_liveness_failures += 1
                if self.consecutive_liveness_failures >= threshold:
                    self.ready = False
            return self.ready

    def _percentile(self, samples, p: float) -> float:
        if not samples:
            return 0.0
        sorted_s = sorted(samples)
        idx = int(len(sorted_s) * p / 100)
        return sorted_s[min(idx, len(sorted_s) - 1)]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "active_sessions":         self.active_sessions,
                "uptime_s":                time.monotonic() - self._start_time,
                "jobs_processed":          dict(self.jobs_processed),
                "jobs_failed":             dict(self.jobs_failed),
                "warm": {
                    "ready":             self.ready,
                    "boot_epoch":        self._boot_epoch,
                    "boot_timestamp":    datetime.fromtimestamp(self._boot_epoch, timezone.utc).isoformat(),
                    "warm_since_epoch":  self.warm_since_epoch,
                    "warm_since":        (
                        datetime.fromtimestamp(self.warm_since_epoch, timezone.utc).isoformat()
                        if self.warm_since_epoch else None
                    ),
                    "boot_duration_ms":  self.boot_duration_ms,
                    "warmup_ms":         self.warmup_ms,
                    "warmup_stage_ms":   dict(self.warmup_stage_ms),
                    "model_load_ms":     dict(self.model_load_ms),
                    "warmup_failures":   self.warmup_failures,
                },
                "liveness": {
                    "checks":               self.liveness_checks,
                    "failures":             self.liveness_failures,
                    "consecutive_failures": self.consecutive_liveness_failures,
                    "ok":                   self.consecutive_liveness_failures == 0,
                    "last_check_epoch":     self.last_liveness_check_epoch,
                    "last_ok_epoch":        self.last_liveness_ok_epoch,
                    "last_ms":              self.last_liveness_ms,
                },
                "latency_ms": {
                    "stt_p50":             self._percentile(self.stt_latencies, 50),
                    "stt_p95":             self._percentile(self.stt_latencies, 95),
                    "translate_p50":       self._percentile(self.translate_latencies, 50),
                    "translate_p95":       self._percentile(self.translate_latencies, 95),
                    "tts_p50":             self._percentile(self.tts_latencies, 50),
                    "tts_p95":             self._percentile(self.tts_latencies, 95),
                    "total_p50":           self._percentile(self.total_latencies, 50),
                    "total_p95":           self._percentile(self.total_latencies, 95),
                    "total_p99":           self._percentile(self.total_latencies, 99),
                },
            }

    def prometheus(self) -> str:
        """Return Prometheus text format for /metrics endpoint."""
        s = self.snapshot()
        w = s["warm"]
        lv = s["liveness"]
        lines = [
            f'linguabridge_active_sessions {s["active_sessions"]}',
            f'linguabridge_uptime_seconds {s["uptime_s"]:.1f}',
            # Warmth / readiness (§4.4) — `ready` flipping or `boot_timestamp`
            # changing signals a cold start / restart to an external monitor.
            f'linguabridge_ready {1 if w["ready"] else 0}',
            f'linguabridge_boot_timestamp_seconds {w["boot_epoch"]:.0f}',
            f'linguabridge_warmup_failures_total {w["warmup_failures"]}',
            # Periodic deep-liveness (§4.4 / S7) — liveness_ok=0 or a stalling
            # last_check timestamp means the GPU path died between calls.
            f'linguabridge_liveness_ok {1 if lv["ok"] else 0}',
            f'linguabridge_liveness_checks_total {lv["checks"]}',
            f'linguabridge_liveness_failures_total {lv["failures"]}',
            f'linguabridge_liveness_consecutive_failures {lv["consecutive_failures"]}',
        ]
        if lv["last_check_epoch"] is not None:
            lines.append(f'linguabridge_liveness_last_check_timestamp_seconds {lv["last_check_epoch"]:.0f}')
        if lv["last_ok_epoch"] is not None:
            lines.append(f'linguabridge_liveness_last_ok_timestamp_seconds {lv["last_ok_epoch"]:.0f}')
        if lv["last_ms"] is not None:
            lines.append(f'linguabridge_liveness_last_ms {lv["last_ms"]:.1f}')
        if w["warm_since_epoch"] is not None:
            lines.append(f'linguabridge_warm_since_timestamp_seconds {w["warm_since_epoch"]:.0f}')
        if w["boot_duration_ms"] is not None:
            lines.append(f'linguabridge_boot_duration_ms {w["boot_duration_ms"]:.1f}')
        if w["warmup_ms"] is not None:
            lines.append(f'linguabridge_warmup_ms {w["warmup_ms"]:.1f}')
        for stage, value in w["warmup_stage_ms"].items():
            lines.append(f'linguabridge_warmup_stage_ms{{stage="{stage}"}} {value:.1f}')
        for stage, count in s["jobs_processed"].items():
            lines.append(f'linguabridge_jobs_processed_total{{stage="{stage}"}} {count}')
        for stage, count in s["jobs_failed"].items():
            lines.append(f'linguabridge_jobs_failed_total{{stage="{stage}"}} {count}')
        for metric, value in s["latency_ms"].items():
            lines.append(f'linguabridge_latency_ms{{metric="{metric}"}} {value:.1f}')
        return "\n".join(lines) + "\n"
