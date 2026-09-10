"""CORE-ID-001: deterministic IDs are stable, and change when input changes."""

from __future__ import annotations

import uuid

import pytest

from app.core.ids import TRACEX_NAMESPACE, deterministic_uuid


def test_same_input_yields_same_id() -> None:
    first = deterministic_uuid("case-1", "evidence-1")
    second = deterministic_uuid("case-1", "evidence-1")
    assert first == second


def test_different_input_yields_different_id() -> None:
    first = deterministic_uuid("case-1", "evidence-1")
    second = deterministic_uuid("case-1", "evidence-2")
    assert first != second


def test_part_boundaries_do_not_collide() -> None:
    """("ab", "c") and ("a", "bc") must not produce the same ID."""
    first = deterministic_uuid("ab", "c")
    second = deterministic_uuid("a", "bc")
    assert first != second


def test_different_namespace_yields_different_id() -> None:
    other_namespace = uuid.uuid4()
    first = deterministic_uuid("case-1", namespace=TRACEX_NAMESPACE)
    second = deterministic_uuid("case-1", namespace=other_namespace)
    assert first != second


def test_requires_at_least_one_part() -> None:
    with pytest.raises(ValueError, match="at least one"):
        deterministic_uuid()


def test_result_is_a_valid_uuid5() -> None:
    result = deterministic_uuid("case-1")
    assert result.version == 5
