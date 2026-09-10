"""Deterministic communication-link candidate construction.

Every function here requires two *real* source observation IDs and
produces a candidate only for one of the explicitly allowed, documented
reasons. None of them ever look at a display name, alias, or
transliteration candidate — `LinkableMessageDescriptor` structurally has
no such field, so a name/transliteration-similarity link is not just
disallowed by policy, it's impossible to construct from here. No
probabilistic score is ever computed. No temporal-proximity link type
exists in this phase.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from itertools import combinations
from uuid import UUID

from app.core.ids import deterministic_uuid
from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.limits import MAX_LINK_CANDIDATES_PER_CALL
from app.modules.communication_processing.linking.models import (
    CommunicationLinkCandidate,
    LinkableMessageDescriptor,
    LinkEvidence,
    LinkStatus,
    LinkType,
)

_CANDIDATE_NAMESPACE_LABEL = "communication_processing.linking.candidate"


def _require_single_case(descriptors: list[LinkableMessageDescriptor]) -> UUID:
    """Candidates are strictly case-scoped; a mixed-case batch is rejected outright."""
    if not descriptors:
        raise ProcessingError(ErrorCode.REQUIRED_FIELD_MISSING, "no descriptors supplied")
    case_ids = {d.case_id for d in descriptors}
    if len(case_ids) > 1:
        raise ProcessingError(
            ErrorCode.CROSS_CASE_INPUT_REJECTED, "descriptors span more than one case_id"
        )
    return next(iter(case_ids))


def _ordered_pair(a: UUID, b: UUID) -> tuple[UUID, UUID]:
    """Canonical (left, right) ordering: the same pair always yields the same
    candidate regardless of which order the caller happened to supply them in."""
    return (a, b) if str(a) <= str(b) else (b, a)


def _build_candidate(
    *,
    case_id: UUID,
    left_id: UUID,
    right_id: UUID,
    link_type: LinkType,
    reason_detail: str,
    created_at: datetime,
) -> CommunicationLinkCandidate:
    left, right = _ordered_pair(left_id, right_id)
    candidate_id = deterministic_uuid(
        _CANDIDATE_NAMESPACE_LABEL, str(case_id), str(left), str(right), link_type.value
    )
    return CommunicationLinkCandidate(
        case_id=case_id,
        candidate_id=candidate_id,
        left_observation_id=left,
        right_observation_id=right,
        link_type=link_type,
        reason_code=link_type.value,
        evidence_refs=(
            LinkEvidence(observation_id=left, detail=reason_detail),
            LinkEvidence(observation_id=right, detail=reason_detail),
        ),
        status=LinkStatus.PROPOSED_FOR_REVIEW,
        created_at=created_at,
    )


def _enforce_limit(candidates: list[CommunicationLinkCandidate]) -> None:
    if len(candidates) > MAX_LINK_CANDIDATES_PER_CALL:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            f"more than {MAX_LINK_CANDIDATES_PER_CALL} candidates in one call",
        )


def find_same_conversation_candidates(
    descriptors: list[LinkableMessageDescriptor], *, now: datetime | None = None
) -> list[CommunicationLinkCandidate]:
    """Two messages sharing a non-empty `conversation_id`, from *different* evidence sources.

    Restricted to different `evidence_id`s deliberately: within one parsed
    evidence file, every message already carries the same `conversation_id`
    by construction — that's parser output, not a fact worth a reviewer's
    attention. The useful signal is cross-evidence corroboration (e.g. the
    same conversation appearing in two independently-submitted exports).
    """
    case_id = _require_single_case(descriptors)
    created_at = now or datetime.now(UTC)

    by_conversation: dict[str, list[LinkableMessageDescriptor]] = {}
    for descriptor in descriptors:
        if descriptor.conversation_id:
            by_conversation.setdefault(descriptor.conversation_id, []).append(descriptor)

    candidates: list[CommunicationLinkCandidate] = []
    for group in by_conversation.values():
        for a, b in combinations(group, 2):
            if a.evidence_id == b.evidence_id:
                continue
            candidates.append(
                _build_candidate(
                    case_id=case_id,
                    left_id=a.observation_id,
                    right_id=b.observation_id,
                    link_type=LinkType.SAME_SOURCE_CONVERSATION,
                    reason_detail="shares conversation_id with another observation from a "
                    "different evidence source",
                    created_at=created_at,
                )
            )
            _enforce_limit(candidates)
    return candidates


def _find_reference_candidates(
    descriptors: list[LinkableMessageDescriptor],
    *,
    get_reference: Callable[[LinkableMessageDescriptor], str | None],
    link_type: LinkType,
    reason_detail: str,
    now: datetime | None,
) -> list[CommunicationLinkCandidate]:
    case_id = _require_single_case(descriptors)
    created_at = now or datetime.now(UTC)

    by_message_id: dict[str, LinkableMessageDescriptor] = {
        d.message_id: d for d in descriptors if d.message_id
    }

    candidates: list[CommunicationLinkCandidate] = []
    for descriptor in descriptors:
        referenced_id = get_reference(descriptor)
        if not referenced_id:
            continue
        target = by_message_id.get(referenced_id)
        if target is None or target.observation_id == descriptor.observation_id:
            continue
        candidates.append(
            _build_candidate(
                case_id=case_id,
                left_id=descriptor.observation_id,
                right_id=target.observation_id,
                link_type=link_type,
                reason_detail=reason_detail,
                created_at=created_at,
            )
        )
        _enforce_limit(candidates)
    return candidates


def find_reply_reference_candidates(
    descriptors: list[LinkableMessageDescriptor], *, now: datetime | None = None
) -> list[CommunicationLinkCandidate]:
    """A message whose `reply_to_message_id` matches another message's `message_id`."""
    return _find_reference_candidates(
        descriptors,
        get_reference=lambda d: d.reply_to_message_id,
        link_type=LinkType.EXPLICIT_REPLY_REFERENCE,
        reason_detail="reply_to_message_id matches another observation's message_id",
        now=now,
    )


def find_message_reference_candidates(
    descriptors: list[LinkableMessageDescriptor], *, now: datetime | None = None
) -> list[CommunicationLinkCandidate]:
    """A message whose `referenced_message_id` matches another message's `message_id`
    (e.g. a forward or quote, distinct from a direct reply)."""
    return _find_reference_candidates(
        descriptors,
        get_reference=lambda d: d.referenced_message_id,
        link_type=LinkType.EXPLICIT_MESSAGE_REFERENCE,
        reason_detail="referenced_message_id matches another observation's message_id",
        now=now,
    )


def _find_shared_token_candidates(
    descriptors: list[LinkableMessageDescriptor],
    *,
    get_token: Callable[[LinkableMessageDescriptor], str | None],
    link_type: LinkType,
    reason_detail: str,
    now: datetime | None,
) -> list[CommunicationLinkCandidate]:
    case_id = _require_single_case(descriptors)
    created_at = now or datetime.now(UTC)

    by_token: dict[str, list[LinkableMessageDescriptor]] = {}
    for descriptor in descriptors:
        token = get_token(descriptor)
        if token:
            by_token.setdefault(token, []).append(descriptor)

    candidates: list[CommunicationLinkCandidate] = []
    for group in by_token.values():
        for a, b in combinations(group, 2):
            candidates.append(
                _build_candidate(
                    case_id=case_id,
                    left_id=a.observation_id,
                    right_id=b.observation_id,
                    link_type=link_type,
                    reason_detail=reason_detail,
                    created_at=created_at,
                )
            )
            _enforce_limit(candidates)
    return candidates


def find_phone_token_candidates(
    descriptors: list[LinkableMessageDescriptor], *, now: datetime | None = None
) -> list[CommunicationLinkCandidate]:
    """Two messages sharing the same `normalized_phone_token`.

    The token must already be normalized by the caller (see `audio/
    transcript_import.py`'s and `social/`'s conservative normalization
    policy) — this function does no normalization of its own and performs
    an exact string match only, never a fuzzy one.
    """
    return _find_shared_token_candidates(
        descriptors,
        get_token=lambda d: d.normalized_phone_token,
        link_type=LinkType.SAME_NORMALIZED_PHONE_TOKEN,
        reason_detail="shares an identical normalized_phone_token with another observation",
        now=now,
    )


def find_handle_token_candidates(
    descriptors: list[LinkableMessageDescriptor], *, now: datetime | None = None
) -> list[CommunicationLinkCandidate]:
    """Two messages sharing the same `exact_handle_token` (exact match only, case-sensitive)."""
    return _find_shared_token_candidates(
        descriptors,
        get_token=lambda d: d.exact_handle_token,
        link_type=LinkType.SAME_EXACT_HANDLE_TOKEN,
        reason_detail="shares an identical exact_handle_token with another observation",
        now=now,
    )
