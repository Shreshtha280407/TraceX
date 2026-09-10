"""Scenario 17: performance metrics are bounded and never include raw media."""

from __future__ import annotations

import time

from app.modules.media_processing.performance import Stopwatch, build_performance_report


def test_stopwatch_stage_records_a_nonnegative_duration() -> None:
    sw = Stopwatch()
    with sw.stage("probe_ms"):
        time.sleep(0.01)
    assert sw.timings.probe_ms >= 0.0
    assert sw.timings.sampling_plan_ms == 0.0  # untouched stage stays exactly zero


def test_stopwatch_stage_accumulates_across_multiple_entries() -> None:
    sw = Stopwatch()
    with sw.stage("analysis_ms"):
        time.sleep(0.005)
    first = sw.timings.analysis_ms
    with sw.stage("analysis_ms"):
        time.sleep(0.005)
    assert sw.timings.analysis_ms > first


def test_stopwatch_total_records_overall_duration() -> None:
    sw = Stopwatch()
    with sw.total(), sw.stage("probe_ms"):
        time.sleep(0.005)
    assert sw.timings.total_ms >= sw.timings.probe_ms


def test_performance_report_never_contains_raw_media_fields() -> None:
    sw = Stopwatch()
    with sw.stage("probe_ms"):
        pass
    report = build_performance_report(sw.timings)
    # The report is exactly {timings, capability} -- no frame/image/video byte
    # payload field exists on either dataclass for raw media to leak into.
    assert set(vars(report.timings).keys()) == {
        "probe_ms",
        "sampling_plan_ms",
        "frame_extraction_ms",
        "analysis_ms",
        "observation_construction_ms",
        "total_ms",
        "frames_requested",
        "frames_extracted",
        "frames_failed",
    }
    assert set(vars(report.capability).keys()) == {
        "cpu_only",
        "nvidia_smi_available",
        "gpu_visible",
        "gpu_name",
        "gpu_memory_mb",
        "ffmpeg_available",
        "ffprobe_available",
    }


def test_performance_report_frame_counters_default_to_zero() -> None:
    sw = Stopwatch()
    report = build_performance_report(sw.timings)
    assert report.timings.frames_requested == 0
    assert report.timings.frames_extracted == 0
    assert report.timings.frames_failed == 0
