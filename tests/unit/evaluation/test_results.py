"""Coverage for benchmark-run validation and the real, frozen metrics spec.

Proof points 9, 10, and 11 from the Phase 7 Part 1 task spec.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.modules.evaluation.catalog import load_model_candidate_catalog
from app.modules.evaluation.models import BenchmarkRunStatus, BenchmarkRunV1, CandidateTask, SplitId
from app.modules.evaluation.results import (
    comparable_runs,
    load_benchmark_metrics_spec,
    require_metric_group_coverage,
)

_NOW = datetime(2026, 9, 15, tzinfo=UTC)
_VALID_SHA256 = "a" * 64


def _run(**overrides: object) -> BenchmarkRunV1:
    fields: dict[str, object] = {
        "schema_version": "v1",
        "run_id": "run-001",
        "candidate_id": "paddleocr-ppocrv5-mobile",
        "dataset_id": "fir_icdar_2023",
        "dataset_release_or_version": "pending_verification",
        "split_id": SplitId.DEVELOPMENT,
        "task": CandidateTask.OCR,
        "runtime_environment": "local-cpu-dev",
        "hardware_profile": "cpu-only-16gb-ram",
        "inference_config_hash": "cfg-" + "b" * 16,
        "metrics": {"latency_ms": 42.0},
        "status": BenchmarkRunStatus.PLANNED,
    }
    fields.update(overrides)
    return BenchmarkRunV1.model_validate(fields)


def test_planned_run_may_omit_artifact_sha256() -> None:
    run = _run(status=BenchmarkRunStatus.PLANNED)
    assert run.artifact_sha256 is None


def test_completed_successful_run_requires_artifact_sha256() -> None:
    """Proof point 9."""
    with pytest.raises(ValidationError, match="artifact_sha256"):
        _run(status=BenchmarkRunStatus.SUCCEEDED, artifact_sha256=None)


def test_completed_successful_run_with_artifact_sha256_is_accepted() -> None:
    run = _run(status=BenchmarkRunStatus.SUCCEEDED, artifact_sha256=_VALID_SHA256)
    assert run.artifact_sha256 == _VALID_SHA256


def test_result_metrics_reject_unsafe_raw_content_via_forbidden_key() -> None:
    """Proof point 10: a metric key that smuggles an identifier through a numeric field."""
    with pytest.raises(ValidationError):
        _run(metrics={"phone_number": 9876543210.0})


def test_result_metrics_reject_a_raw_string_value() -> None:
    """Proof point 10: metrics are type-constrained to float/int/None, never raw text."""
    with pytest.raises(ValidationError):
        _run(metrics={"note": "some raw transcript content here"})


def test_result_metrics_accept_ordinary_numeric_values() -> None:
    run = _run(
        metrics={"character_error_rate": 0.12, "latency_ms": 88, "field_extraction_f1": None}
    )
    assert run.metrics["field_extraction_f1"] is None


def test_failure_reason_rejects_a_leaked_credential() -> None:
    with pytest.raises(ValidationError):
        _run(
            status=BenchmarkRunStatus.FAILED,
            failure_reason_safe="connection failed, password=hunter2",
        )


def test_all_required_metric_groups_are_represented() -> None:
    """Proof point 11: every task used by a catalogued candidate has a metric group."""
    catalog = load_model_candidate_catalog()
    spec = load_benchmark_metrics_spec()
    tasks_in_catalog = {candidate.task for candidate in catalog.candidates}
    tasks_in_spec = {group.task for group in spec.metric_groups}
    missing = tasks_in_catalog - tasks_in_spec
    assert not missing, f"tasks with no metric group: {missing}"


def test_require_metric_group_coverage_rejects_a_run_missing_a_required_metric() -> None:
    spec = load_benchmark_metrics_spec()
    run = _run(task=CandidateTask.OCR, metrics={"latency_ms": 10.0})
    with pytest.raises(ValueError, match="missing required metrics"):
        require_metric_group_coverage(run, spec)


def test_require_metric_group_coverage_accepts_a_fully_covered_run() -> None:
    spec = load_benchmark_metrics_spec()
    group = spec.for_task(CandidateTask.OCR)
    assert group is not None
    run = _run(task=CandidateTask.OCR, metrics=dict.fromkeys(group.required_metric_names))
    require_metric_group_coverage(run, spec)  # does not raise


def test_comparable_runs_requires_same_dataset_and_split() -> None:
    """Frozen rule 5: a comparison is invalid across a different dataset or split."""
    base = _run(dataset_id="fir_icdar_2023", split_id=SplitId.DEVELOPMENT)
    same = _run(run_id="run-002", dataset_id="fir_icdar_2023", split_id=SplitId.DEVELOPMENT)
    different_dataset = _run(
        run_id="run-003", dataset_id="gomask_voice_cdr", split_id=SplitId.DEVELOPMENT
    )
    different_split = _run(run_id="run-004", dataset_id="fir_icdar_2023", split_id=SplitId.HOLDOUT)
    assert comparable_runs(base, same) is True
    assert comparable_runs(base, different_dataset) is False
    assert comparable_runs(base, different_split) is False
