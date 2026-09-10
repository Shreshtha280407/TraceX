"""Canonical serialization: stable key order, type handling, SHA-256 helper."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from app.contracts.common import ReviewStatus
from app.core.canonical import canonical_bytes, canonical_sha256
from tests.fixtures.factories import make_entity


def test_canonical_json_stable_despite_key_order() -> None:
    a = {"b": 1, "a": 2, "c": {"z": 1, "y": 2}}
    b = {"a": 2, "c": {"y": 2, "z": 1}, "b": 1}
    assert canonical_bytes(a) == canonical_bytes(b)


def test_canonical_bytes_changes_with_content() -> None:
    assert canonical_bytes({"a": 1}) != canonical_bytes({"a": 2})


def test_canonical_sha256_matches_manual_hash() -> None:
    payload = {"a": 1, "b": [1, 2, 3]}
    expected = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    assert canonical_sha256(payload) == expected


def test_canonical_bytes_handles_uuid_datetime_enum() -> None:
    entity_id = uuid4()
    payload = {
        "id": entity_id,
        "ts": datetime(2026, 1, 1, tzinfo=UTC),
        "status": ReviewStatus.CONFIRMED,
    }
    data = canonical_bytes(payload)
    assert str(entity_id).encode() in data
    assert b'"confirmed"' in data
    assert b"2026-01-01" in data


def test_canonical_bytes_handles_nested_pydantic_model() -> None:
    entity = make_entity()
    data = canonical_bytes(entity)
    assert str(entity.entity_id).encode() in data
    assert entity.canonical_label.encode() in data


def test_canonical_bytes_is_deterministic_across_calls() -> None:
    entity = make_entity()
    assert canonical_bytes(entity) == canonical_bytes(entity)
