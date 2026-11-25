from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator


@dataclass
class StageTiming:
    run_id: str
    stage: str
    elapsed_ms: float
    ok: bool
    detail: str = ""


@dataclass
class MilestoneTiming:
    run_id: str
    milestone: str
    t_ms: float  # absolute time.perf_counter() * 1000
    detail: str = ""


class LatencyLog:
    def __init__(self, log_path: Path) -> None:
        self.run_id = uuid.uuid4().hex[:12]
        self.log_path = log_path
        self.timings: list[StageTiming] = []
        self.milestones: list[MilestoneTiming] = []
        self.pipeline_start: float | None = None
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def measure(self, stage: str, detail: str = "") -> Iterator[None]:
        start = time.perf_counter()
        ok = False
        try:
            yield
            ok = True
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            timing = StageTiming(
                run_id=self.run_id,
                stage=stage,
                elapsed_ms=round(elapsed_ms, 2),
                ok=ok,
                detail=detail,
            )
            self.timings.append(timing)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(timing), ensure_ascii=True) + "\n")

    def record_milestone(self, milestone: str, t_abs: float, detail: str = "") -> None:
        m = MilestoneTiming(
            run_id=self.run_id,
            milestone=milestone,
            t_ms=round(t_abs * 1000, 2),
            detail=detail,
        )
        self.milestones.append(m)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(m), ensure_ascii=True) + "\n")

    def first_byte_ms(self) -> float | None:
        if self.pipeline_start is None:
            return None
        for m in self.milestones:
            if m.milestone == "t_first_audio_byte":
                return round((m.t_ms / 1000 - self.pipeline_start) * 1000, 2)
        return None

    def total_ms(self) -> float:
        return round(sum(item.elapsed_ms for item in self.timings), 2)

