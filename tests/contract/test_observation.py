"""Contract tests for ObservationV1. See docs/qa/test-matrix.md (CORE-CONTRACT-001/002)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.contracts.observation import ObservationV1
from tests.fixtures.factories import assert_roundtrips, make_observation, make_source_locator


def test_valid_observation_parses() -> None:
    observation = make_observation()
    assert observation.schema_version == "v1"


def test_observation_roundtrips() -> None:
    assert_roundtrips(make_observation())


def test_observation_unsupported_schema_version_is_rejected() -> None:
    payload = make_observation().model_dump(mode="json")
    payload["schema_version"] = "v0"
    with pytest.raises(ValidationError):
        ObservationV1(**payload)


@pytest.mark.parametrize("confidence", [-0.01, 1.01, -1.0, 2.0])
def test_observation_invalid_confidence_is_rejected(confidence: float) -> None:
    with pytest.raises(ValidationError):
        make_observation(extraction_confidence=confidence)


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
def test_observation_boundary_confidence_is_valid(confidence: float) -> None:
    observation = make_observation(extraction_confidence=confidence)
    assert observation.extraction_confidence == confidence


def test_observation_empty_source_locator_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_observation(source_locator={})


def test_observation_source_locator_invalid_time_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_source_locator(time_start_ms=200, time_end_ms=100)
