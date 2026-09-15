"""Contract-level safety rules for Phase 7 Part 1 evaluation models.

Proof points 2, 3, 4, and 15 from the task spec.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.modules.evaluation.models import (
    DatasetEvaluationStatus,
    DatasetManifestEntryV1,
    DatasetRole,
    EvaluationModality,
    GitStoragePolicy,
    LicenseStatus,
    RedistributionStatus,
    TeamOwner,
    default_evidence_provenance_report_requirements,
    default_investigator_lead_report_requirements,
)

_NOW = datetime(2026, 9, 15, tzinfo=UTC)


def _entry(**overrides: object) -> DatasetManifestEntryV1:
    fields: dict[str, object] = {
        "schema_version": "v1",
        "dataset_id": "example_dataset",
        "display_name": "Example Dataset",
        "role": DatasetRole.PRIMARY_BENCHMARK,
        "owner": TeamOwner.NIPUN,
        "modalities": (EvaluationModality.DOCUMENT,),
        "allowed_tasks": ("example task",),
        "prohibited_claims": ("example prohibited claim",),
        "license_status": LicenseStatus.PENDING_VERIFICATION,
        "license_notes": "example notes",
        "local_path_placeholder": "local-data/example_dataset/",
        "git_storage_policy": GitStoragePolicy.EXCLUDED_MANIFEST_ONLY,
        "redistribution_status": RedistributionStatus.PENDING_VERIFICATION,
        "evaluation_status": DatasetEvaluationStatus.PLANNED,
        "known_limitations": ("example limitation",),
    }
    fields.update(overrides)
    return DatasetManifestEntryV1.model_validate(fields)


def test_unsupported_schema_version_is_rejected() -> None:
    """Proof point 2."""
    with pytest.raises(ValidationError):
        _entry(schema_version="v2")


def test_invalid_role_is_rejected() -> None:
    """Proof point 3 (role)."""
    with pytest.raises(ValidationError):
        _entry(role="not_a_real_role")


def test_invalid_owner_is_rejected() -> None:
    """Proof point 3 (owner)."""
    with pytest.raises(ValidationError):
        _entry(owner="not_a_team_member")


def test_invalid_storage_policy_is_rejected() -> None:
    """Proof point 3 (git_storage_policy)."""
    with pytest.raises(ValidationError):
        _entry(git_storage_policy="commit_everything")


def test_absolute_local_path_is_rejected() -> None:
    """Proof point 4."""
    with pytest.raises(ValidationError):
        _entry(local_path_placeholder="/etc/passwd")


def test_home_relative_local_path_is_rejected() -> None:
    """Proof point 4."""
    with pytest.raises(ValidationError):
        _entry(local_path_placeholder="~/secret-data/")


def test_traversal_local_path_is_rejected() -> None:
    """Proof point 4."""
    with pytest.raises(ValidationError):
        _entry(local_path_placeholder="local-data/../../etc/passwd")


def test_local_path_outside_allowed_roots_is_rejected() -> None:
    """Proof point 4."""
    with pytest.raises(ValidationError):
        _entry(local_path_placeholder="some-other-directory/example_dataset/")


def test_safe_local_path_is_accepted() -> None:
    entry = _entry(local_path_placeholder="private-evaluation/example_dataset/")
    assert entry.local_path_placeholder == "private-evaluation/example_dataset/"


def test_both_investigator_report_requirements_are_represented() -> None:
    """Proof point 15."""
    evidence_report = default_evidence_provenance_report_requirements()
    lead_report = default_investigator_lead_report_requirements()
    assert evidence_report.report_type == "evidence_and_provenance_report"
    assert lead_report.report_type == "investigator_intelligence_lead_report"
    assert len(evidence_report.required_elements) > 0
    assert len(lead_report.required_elements) > 0
    assert "guilt" in lead_report.prohibited_language


def test_investigator_lead_report_must_prohibit_guilt_language() -> None:
    from app.modules.evaluation.models import InvestigatorLeadReportRequirementsV1

    with pytest.raises(ValidationError, match="guilt"):
        InvestigatorLeadReportRequirementsV1(
            schema_version="v1",
            required_elements=("case-scoped candidate links",),
            prohibited_language=("bribery",),
        )
