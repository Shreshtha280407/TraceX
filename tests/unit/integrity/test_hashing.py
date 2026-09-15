"""Pure-function coverage for `app.modules.integrity.hashing`.

Proof points 1, 2, 4, 5, 12 from the Phase 6 task spec: deterministic leaf/
root hashes, dictionary-insertion-order independence, sequence-determined
leaf order, the frozen duplicate-last-leaf odd-node rule, and swapped-leaf-
order detection. The DB-backed proof points (3, 6-11, 13-15, 18) live in
`tests/integration/integrity/`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.core.canonical import canonical_sha256
from app.modules.integrity.hashing import build_merkle_root, leaf_hash, parent_hash
from app.modules.integrity.models import IntegrityEventKind, IntegrityEventRecord

_NOW = datetime(2026, 9, 15, tzinfo=UTC)


def _event(sequence_number: int, **overrides: object) -> IntegrityEventRecord:
    fields: dict[str, object] = {
        "integrity_event_id": uuid4(),
        "case_id": uuid4(),
        "event_kind": IntegrityEventKind.EVIDENCE_REGISTERED,
        "subject_type": "evidence",
        "subject_id": "evidence-1",
        "canonical_payload_sha256": "a" * 64,
        "payload_schema_version": "v1",
        "source_created_at": _NOW,
        "idempotency_key": "key-1",
        "sequence_number": sequence_number,
        "created_at": _NOW,
    }
    fields.update(overrides)
    return IntegrityEventRecord.model_validate(fields)


def test_leaf_hash_is_deterministic_for_identical_events() -> None:
    event_id = uuid4()
    case_id = uuid4()
    a = _event(1, integrity_event_id=event_id, case_id=case_id)
    b = _event(1, integrity_event_id=event_id, case_id=case_id)
    assert leaf_hash(a) == leaf_hash(b)


def test_leaf_hash_changes_when_any_field_changes() -> None:
    base = _event(1)
    changed = _event(1, canonical_payload_sha256="b" * 64)
    assert leaf_hash(base) != leaf_hash(changed)


def test_merkle_root_deterministic_for_same_ordered_leaf_range() -> None:
    events = [_event(i) for i in range(1, 6)]
    leaves = [leaf_hash(e) for e in events]
    assert build_merkle_root(leaves) == build_merkle_root(leaves)


def test_canonical_metadata_hash_is_independent_of_dict_insertion_order() -> None:
    """Proof point 2: the exact hashing call `IntegrityRepository.record_event` makes."""
    forward = {"a": 1, "b": {"x": 1, "y": 2}, "c": [1, 2, 3]}
    reversed_insertion = {"c": [1, 2, 3], "b": {"y": 2, "x": 1}, "a": 1}
    assert canonical_sha256(forward) == canonical_sha256(reversed_insertion)


def test_odd_leaf_count_duplicates_the_final_leaf() -> None:
    """Proof point 5: the frozen odd-node rule, not zero-padding."""
    leaves = [leaf_hash(_event(i)) for i in range(1, 4)]  # 3 leaves: odd
    expected_level = [leaves[0], leaves[1], leaves[2], leaves[2]]
    expected_root = parent_hash(
        parent_hash(expected_level[0], expected_level[1]),
        parent_hash(expected_level[2], expected_level[3]),
    )
    assert build_merkle_root(leaves) == expected_root


def test_root_depends_on_leaf_order_not_only_leaf_set() -> None:
    """Proof point 4 and 12: sequence-determined order; a swap changes the root."""
    events = [_event(i) for i in range(1, 5)]
    in_order = [leaf_hash(e) for e in events]
    swapped = list(in_order)
    swapped[0], swapped[1] = swapped[1], swapped[0]
    assert build_merkle_root(in_order) != build_merkle_root(swapped)


def test_build_merkle_root_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="zero leaves"):
        build_merkle_root([])
