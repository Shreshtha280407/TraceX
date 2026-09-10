"""Scenarios 22-27: deterministic, case-scoped, review-only communication-link candidates."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.linking.deterministic import (
    find_handle_token_candidates,
    find_message_reference_candidates,
    find_phone_token_candidates,
    find_reply_reference_candidates,
    find_same_conversation_candidates,
)
from app.modules.communication_processing.linking.models import (
    LinkableMessageDescriptor,
    LinkStatus,
    LinkType,
)


def _descriptor(case_id: object = None, **overrides: object) -> LinkableMessageDescriptor:
    data: dict[str, object] = {
        "observation_id": uuid4(),
        "case_id": case_id or uuid4(),
        "evidence_id": uuid4(),
    }
    data.update(overrides)
    return LinkableMessageDescriptor(**data)  # type: ignore[arg-type]


def test_reply_reference_candidate_preserves_evidence_and_review_status() -> None:
    """Scenario 27."""
    case_id = uuid4()
    a = _descriptor(case_id=case_id, message_id="m1")
    b = _descriptor(case_id=case_id, message_id="m2", reply_to_message_id="m1")

    candidates = find_reply_reference_candidates([a, b])
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.link_type == LinkType.EXPLICIT_REPLY_REFERENCE
    assert candidate.status == LinkStatus.PROPOSED_FOR_REVIEW
    assert candidate.case_id == case_id
    assert {e.observation_id for e in candidate.evidence_refs} == {
        a.observation_id,
        b.observation_id,
    }
    assert {candidate.left_observation_id, candidate.right_observation_id} == {
        a.observation_id,
        b.observation_id,
    }


def test_message_reference_candidate() -> None:
    case_id = uuid4()
    a = _descriptor(case_id=case_id, message_id="m1")
    b = _descriptor(case_id=case_id, message_id="m2", referenced_message_id="m1")
    candidates = find_message_reference_candidates([a, b])
    assert len(candidates) == 1
    assert candidates[0].link_type == LinkType.EXPLICIT_MESSAGE_REFERENCE


def test_candidate_ids_are_deterministic() -> None:
    """Scenario 23."""
    case_id = uuid4()
    a = _descriptor(case_id=case_id, message_id="m1")
    b = _descriptor(case_id=case_id, message_id="m2", reply_to_message_id="m1")

    first = find_reply_reference_candidates([a, b])
    second = find_reply_reference_candidates([a, b])
    assert first[0].candidate_id == second[0].candidate_id


def test_candidate_ids_are_order_independent() -> None:
    """Same pair, opposite input order -> the same candidate_id (proper dedup)."""
    case_id = uuid4()
    a = _descriptor(case_id=case_id, message_id="m1")
    b = _descriptor(case_id=case_id, message_id="m2", reply_to_message_id="m1")

    forward = find_reply_reference_candidates([a, b])
    backward = find_reply_reference_candidates([b, a])
    assert forward[0].candidate_id == backward[0].candidate_id


def test_candidate_id_changes_with_meaningful_input_change() -> None:
    """Scenario 24."""
    case_id = uuid4()
    a = _descriptor(case_id=case_id, message_id="m1")
    b = _descriptor(case_id=case_id, message_id="m2", reply_to_message_id="m1")
    c = _descriptor(case_id=case_id, message_id="m3", reply_to_message_id="m1")

    baseline = find_reply_reference_candidates([a, b])[0]
    changed_right = find_reply_reference_candidates([a, c])[0]
    assert baseline.candidate_id != changed_right.candidate_id


def test_cross_case_descriptors_are_rejected() -> None:
    """Scenario 25."""
    a = _descriptor(message_id="m1")
    b = _descriptor(message_id="m2", reply_to_message_id="m1")  # different case_id
    with pytest.raises(ProcessingError) as exc_info:
        find_reply_reference_candidates([a, b])
    assert exc_info.value.code == ErrorCode.CROSS_CASE_INPUT_REJECTED


def test_no_link_type_exists_for_display_name_similarity() -> None:
    """Scenario 26: LinkableMessageDescriptor has no display-name field at all,
    and LinkType has no name/transliteration-similarity member."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(LinkableMessageDescriptor)}
    assert "display_name" not in field_names
    assert "name" not in field_names
    assert "alias" not in field_names
    link_type_values = {member.value for member in LinkType}
    assert not any("name" in value or "alias" in value for value in link_type_values)


def test_phone_token_candidate_requires_exact_match() -> None:
    case_id = uuid4()
    a = _descriptor(case_id=case_id, normalized_phone_token="9876543210")
    b = _descriptor(case_id=case_id, normalized_phone_token="9876543210")
    c = _descriptor(case_id=case_id, normalized_phone_token="9123456780")

    candidates = find_phone_token_candidates([a, b, c])
    assert len(candidates) == 1
    assert candidates[0].link_type == LinkType.SAME_NORMALIZED_PHONE_TOKEN


def test_handle_token_candidate_requires_exact_match() -> None:
    case_id = uuid4()
    a = _descriptor(case_id=case_id, exact_handle_token="@alice")
    b = _descriptor(case_id=case_id, exact_handle_token="@alice")
    candidates = find_handle_token_candidates([a, b])
    assert len(candidates) == 1
    assert candidates[0].link_type == LinkType.SAME_EXACT_HANDLE_TOKEN


def test_same_conversation_requires_different_evidence_source() -> None:
    case_id = uuid4()
    ev1, ev2 = uuid4(), uuid4()
    a = _descriptor(case_id=case_id, evidence_id=ev1, conversation_id="c1")
    b = _descriptor(
        case_id=case_id, evidence_id=ev1, conversation_id="c1"
    )  # same evidence: no candidate
    candidates = find_same_conversation_candidates([a, b])
    assert candidates == []

    c = _descriptor(case_id=case_id, evidence_id=ev2, conversation_id="c1")
    candidates_cross = find_same_conversation_candidates([a, c])
    assert len(candidates_cross) == 1
    assert candidates_cross[0].link_type == LinkType.SAME_SOURCE_CONVERSATION


def test_no_candidates_from_empty_input_raises() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        find_reply_reference_candidates([])
    assert exc_info.value.code == ErrorCode.REQUIRED_FIELD_MISSING
