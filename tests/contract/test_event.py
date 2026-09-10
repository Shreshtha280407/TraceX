"""Contract tests for EventV1. See docs/qa/test-matrix.md (CORE-CONTRACT-001/002)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.contracts.common import TimeWindow
from app.contracts.event import EventV1
from tests.fixtures.factories import FIXED_TIME, assert_roundtrips, make_event


def test_valid_event_parses() -> None:
    event = make_event()
    assert event.schema_version == "v1"


def test_event_roundtrips() -> None:
    assert_roundtrips(make_event())


def test_event_unsupported_schema_version_is_rejected() -> None:
    payload = make_event().model_dump(mode="json")
    payload["schema_version"] = "v2"
    with pytest.raises(ValidationError):
        EventV1(**payload)


@pytest.mark.parametrize("confidence", [-0.5, 1.5])
def test_event_invalid_confidence_is_rejected(confidence: float) -> None:
    with pytest.raises(ValidationError):
        make_event(confidence=confidence)


def test_event_requires_evidence_refs() -> None:
    with pytest.raises(ValidationError):
        make_event(evidence_refs=[])


def test_event_requires_participants() -> None:
    with pytest.raises(ValidationError):
        make_event(participant_entity_ids=[])


def test_event_requires_event_time_or_window() -> None:
    """Events are time-bounded occurrences, never timeless entity-to-entity edges."""
    with pytest.raises(ValidationError):
        make_event(event_time=None, time_window=None)


def test_event_with_only_time_window_is_valid() -> None:
    window = TimeWindow(start=FIXED_TIME, end=FIXED_TIME)
    event = make_event(event_time=None, time_window=window)
    assert event.time_window == window
