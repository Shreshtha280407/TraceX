"""Transparent pipeline timing -- measured values only, never a promised throughput.

Per `docs/architecture/media-processing-v1.md`, this module reports actual
measured stage timings and hardware context; it never claims a fixed
throughput figure (e.g. "one hour of CCTV in N minutes"). `Stopwatch` uses
`time.monotonic()`, which is immune to wall-clock adjustments -- the only
property that matters for measuring elapsed durations.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from app.modules.media_processing.capability import CapabilityReport, detect_capability


@dataclass
class StageTimings:
    """Measured wall-clock duration (milliseconds) of each pipeline stage.

    All fields default to `0`/`0.0` so a caller that skips a stage (e.g. an
    image has no sampling-plan stage) does not need to fabricate a value --
    an unset stage genuinely took no time because it never ran.
    """

    probe_ms: float = 0.0
    sampling_plan_ms: float = 0.0
    frame_extraction_ms: float = 0.0
    analysis_ms: float = 0.0
    observation_construction_ms: float = 0.0
    total_ms: float = 0.0
    frames_requested: int = 0
    frames_extracted: int = 0
    frames_failed: int = 0


@dataclass(frozen=True)
class PerformanceReport:
    """A complete, honest performance summary for one processing run."""

    timings: StageTimings
    capability: CapabilityReport


@contextmanager
def _stopwatch() -> Iterator[list[float]]:
    """Yields a one-element list; on exit it holds the elapsed milliseconds."""
    elapsed: list[float] = [0.0]
    start = time.monotonic()
    try:
        yield elapsed
    finally:
        elapsed[0] = (time.monotonic() - start) * 1000.0


@dataclass
class Stopwatch:
    """A named-stage timer that accumulates into a `StageTimings` field.

    Usage: `with stopwatch.stage("probe_ms"): ...` -- adds (rather than
    overwrites) the elapsed time, so a stage entered more than once in one
    run (e.g. per-frame extraction) accumulates correctly.
    """

    timings: StageTimings = field(default_factory=StageTimings)

    @contextmanager
    def stage(self, field_name: str) -> Iterator[None]:
        with _stopwatch() as elapsed:
            yield
        current = getattr(self.timings, field_name)
        setattr(self.timings, field_name, current + elapsed[0])

    @contextmanager
    def total(self) -> Iterator[None]:
        with _stopwatch() as elapsed:
            yield
        self.timings.total_ms = elapsed[0]


def build_performance_report(timings: StageTimings) -> PerformanceReport:
    """Pair measured timings with a fresh local capability snapshot."""
    return PerformanceReport(timings=timings, capability=detect_capability())
