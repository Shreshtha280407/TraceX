"""Contract-level safety rules for Phase 6 internal integrity models.

Proof point 16 (in part): a producer that tries to pass source-like content
as `canonical_metadata` is rejected at the submission boundary, before it
could ever reach hashing or storage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.integrity.models import IntegrityEventKind, IntegrityEventSubmission

_NOW = datetime(2026, 9, 15, tzinfo=UTC)


def _submission(**overrides: object) -> IntegrityEventSubmission:
    fields: dict[str, object] = {
        "case_id": uuid4(),
        "event_kind": IntegrityEventKind.EVIDENCE_REGISTERED,
        "subject_type": "evidence",
        "subject_id": "evidence-1",
        "canonical_metadata": {},
        "payload_schema_version": "v1",
        "source_created_at": _NOW,
        "idempotency_key": "evidence-1",
    }
    fields.update(overrides)
    return IntegrityEventSubmission.model_validate(fields)


def test_accepts_safe_structural_metadata() -> None:
    submission = _submission(canonical_metadata={"sha256": "a" * 64, "observation_count": 3})
    assert submission.canonical_metadata["observation_count"] == 3


def test_rejects_transcript_shaped_key() -> None:
    with pytest.raises(ValidationError, match="prohibited key"):
        _submission(canonical_metadata={"transcript": "hello"})


def test_rejects_nested_secret_shaped_key() -> None:
    with pytest.raises(ValidationError, match="prohibited key"):
        _submission(canonical_metadata={"nested": {"object_uri": "s3://bucket/key"}})


def test_rejects_oversized_string_value() -> None:
    with pytest.raises(ValidationError, match="bounded"):
        _submission(canonical_metadata={"note": "x" * 2_001})


def test_idempotency_key_pattern_is_enforced() -> None:
    with pytest.raises(ValidationError):
        _submission(idempotency_key="has a space")


def test_forward_compatible_event_kinds_exist_without_a_workflow() -> None:
    """`review_decision`/`hypothesis_action` are representable now; no workflow ships yet."""
    assert IntegrityEventKind.REVIEW_DECISION.value == "review_decision"
    assert IntegrityEventKind.HYPOTHESIS_ACTION.value == "hypothesis_action"
    _submission(event_kind=IntegrityEventKind.REVIEW_DECISION, subject_type="review")
