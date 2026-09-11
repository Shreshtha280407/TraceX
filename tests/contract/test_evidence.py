"""Contract tests for EvidenceRecordV1. See docs/qa/test-matrix.md (CORE-CONTRACT-001)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.contracts.evidence import EvidenceRecordV1, SourceType
from tests.fixtures.factories import assert_roundtrips, make_evidence_record


def test_valid_evidence_record_parses() -> None:
    record = make_evidence_record()
    assert record.schema_version == "v1"


def test_evidence_record_roundtrips() -> None:
    assert_roundtrips(make_evidence_record())


def test_evidence_record_unsupported_schema_version_is_rejected() -> None:
    payload = make_evidence_record().model_dump(mode="json")
    payload["schema_version"] = "v2"
    with pytest.raises(ValidationError):
        EvidenceRecordV1(**payload)


def test_evidence_record_invalid_sha256_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_evidence_record(sha256="not-a-sha256")


def test_evidence_record_rejects_unknown_fields() -> None:
    payload = make_evidence_record().model_dump(mode="json")
    payload["unexpected_field"] = "surprise"
    with pytest.raises(ValidationError):
        EvidenceRecordV1(**payload)


@pytest.mark.parametrize("source_type", [SourceType.STRUCTURED_TABULAR, SourceType.STRUCTURED_JSON])
def test_evidence_record_accepts_phase_2_3_structured_source_types(
    source_type: SourceType,
) -> None:
    """Phase 2.3: additive enum values -- every pre-existing value stays valid too."""
    assert_roundtrips(make_evidence_record(source_type=source_type))
