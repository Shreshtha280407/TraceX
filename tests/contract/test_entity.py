"""Contract tests for EntityV1. See docs/qa/test-matrix.md (CORE-CONTRACT-001)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.contracts.entity import EntityV1
from tests.fixtures.factories import assert_roundtrips, make_entity


def test_valid_entity_parses() -> None:
    entity = make_entity()
    assert entity.schema_version == "v1"
    assert entity.review_status == "unreviewed"


def test_entity_roundtrips() -> None:
    assert_roundtrips(make_entity())


def test_entity_unsupported_schema_version_is_rejected() -> None:
    payload = make_entity().model_dump(mode="json")
    payload["schema_version"] = "v2"
    with pytest.raises(ValidationError):
        EntityV1(**payload)


def test_entity_requires_at_least_one_source_observation() -> None:
    with pytest.raises(ValidationError):
        make_entity(created_from_observation_ids=[])
