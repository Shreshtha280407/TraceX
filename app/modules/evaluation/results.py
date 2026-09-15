"""Benchmark-metrics specification loading and benchmark-run comparison/validation.

No actual benchmark results exist in Phase 7 Part 1 -- there is no
`benchmark-results.v1.json` to load, only this module's typed `BenchmarkRunV1`
(in `models.py`) and the frozen metric-group requirements a future run must
satisfy, loaded from `configs/benchmarks/benchmark-metrics.v1.json`.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.modules.evaluation.models import BenchmarkMetricsSpecV1, BenchmarkRunV1, comparable_runs

REPO_ROOT = Path(__file__).resolve().parents[3]
BENCHMARK_METRICS_SPEC_PATH = REPO_ROOT / "configs" / "benchmarks" / "benchmark-metrics.v1.json"

__all__ = [
    "BENCHMARK_METRICS_SPEC_PATH",
    "comparable_runs",
    "load_benchmark_metrics_spec",
    "require_metric_group_coverage",
]


def load_benchmark_metrics_spec(path: Path = BENCHMARK_METRICS_SPEC_PATH) -> BenchmarkMetricsSpecV1:
    data = json.loads(path.read_text(encoding="utf-8"))
    return BenchmarkMetricsSpecV1.model_validate(data)


def require_metric_group_coverage(run: BenchmarkRunV1, spec: BenchmarkMetricsSpecV1) -> None:
    """Frozen rule 4 (in part): every metric name the run's task requires
    must be a key in `run.metrics` -- the *value* may be `None` when a
    metric genuinely can't be measured (e.g. no ground-truth labels for
    this dataset), but the key's presence is what makes two runs of the
    same task comparable at all.
    """
    group = spec.for_task(run.task)
    if group is None:
        raise ValueError(f"no metric group defined for task '{run.task.value}'")
    missing = [name for name in group.required_metric_names if name not in run.metrics]
    if missing:
        raise ValueError(f"benchmark run '{run.run_id}' is missing required metrics: {missing}")
