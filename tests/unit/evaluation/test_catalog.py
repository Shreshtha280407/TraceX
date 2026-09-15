"""Coverage for the real, frozen Model Candidate Catalog V1.

Proof point 8 from the Phase 7 Part 1 task spec.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.modules.evaluation.catalog import candidates_for_task, load_model_candidate_catalog
from app.modules.evaluation.models import (
    CandidateSelectionStatus,
    CandidateTask,
    ExecutionTarget,
    ModelCandidateV1,
    TeamOwner,
)


def _candidate(**overrides: object) -> ModelCandidateV1:
    fields: dict[str, object] = {
        "schema_version": "v1",
        "candidate_id": "example-candidate",
        "owner": TeamOwner.NIPUN,
        "task": CandidateTask.OCR,
        "framework": "example-framework",
        "model_family": "example-family",
        "variant": "default",
        "execution_target": ExecutionTarget.CPU,
        "license_status": "pending_verification",
        "selection_status": CandidateSelectionStatus.CANDIDATE,
        "required_metrics": ("latency_ms",),
        "known_limitations": ("not yet benchmarked",),
    }
    fields.update(overrides)
    return ModelCandidateV1.model_validate(fields)


def test_model_candidate_cannot_start_as_selected() -> None:
    """Proof point 8."""
    with pytest.raises(ValidationError, match="never 'selected'"):
        _candidate(selection_status="selected")


def test_model_candidate_may_start_as_candidate_or_conditional() -> None:
    assert (
        _candidate(selection_status="candidate").selection_status
        == CandidateSelectionStatus.CANDIDATE
    )
    assert (
        _candidate(selection_status="conditional").selection_status
        == CandidateSelectionStatus.CONDITIONAL
    )


def test_real_model_candidate_catalog_loads_and_has_no_selected_candidate() -> None:
    catalog = load_model_candidate_catalog()
    assert len(catalog.candidates) >= 15
    for candidate in catalog.candidates:
        assert candidate.selection_status != CandidateSelectionStatus.SELECTED


def test_every_candidate_fallback_reference_resolves_within_the_catalog() -> None:
    catalog = load_model_candidate_catalog()
    known_ids = {candidate.candidate_id for candidate in catalog.candidates}
    for candidate in catalog.candidates:
        if candidate.fallback_candidate_id is not None:
            assert candidate.fallback_candidate_id in known_ids


def test_correlation_task_includes_the_frozen_rules_baseline_and_three_ml_candidates() -> None:
    catalog = load_model_candidate_catalog()
    correlation_candidates = candidates_for_task(catalog, CandidateTask.CORRELATION)
    frameworks = {candidate.framework for candidate in correlation_candidates}
    assert "internal" in frameworks  # the frozen rules baseline
    assert {"scikit-learn", "XGBoost", "LightGBM"} <= frameworks


def test_tracking_task_has_no_face_or_reid_candidate() -> None:
    """No biometric identity model is ever a candidate for tracking."""
    catalog = load_model_candidate_catalog()
    tracking_candidates = candidates_for_task(catalog, CandidateTask.TRACKING)
    for candidate in tracking_candidates:
        combined = f"{candidate.model_family} {candidate.variant}".lower()
        assert "face" not in combined
        assert "reid" not in combined
        assert "re-id" not in combined
