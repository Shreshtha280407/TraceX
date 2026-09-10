"""Contract tests for EvidenceRecordV1. See docs/qa/test-matrix.md (CORE-CONTRACT-001)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.contracts.evidence import EvidenceRecordV1
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
