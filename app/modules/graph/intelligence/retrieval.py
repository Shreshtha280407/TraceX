"""Case-scoped, bounded deterministic candidate retrieval.

The local vector is feature hashing over normalized tokens. It is a weak
retrieval aid, not a semantic model and never an identity assertion.
"""

from __future__ import annotations

import hashlib
import math
import re
from itertools import combinations

from app.modules.graph.intelligence.models import (
    ObservationDescriptor,
    RetrievalReason,
    RetrievedCandidate,
)

VECTOR_PROVIDER = "local_hashed_token_vector"
VECTOR_PROVIDER_VERSION = "v1"
VECTOR_DIMENSIONS = 32
MAX_CANDIDATES = 200
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_IDENTIFIER_TYPES = frozenset(
    {
        "phone",
        "email",
        "vehicle_registration",
        "device_id",
        "account",
        "platform_handle",
    }
)


def _single_case(items: list[ObservationDescriptor]) -> None:
    if len({item.case_id for item in items}) > 1:
        raise ValueError("candidate retrieval input spans multiple cases")


def _ordered(
    left: ObservationDescriptor, right: ObservationDescriptor
) -> tuple[ObservationDescriptor, ObservationDescriptor]:
    return (left, right) if str(left.observation_id) < str(right.observation_id) else (right, left)


def _same_origin_event(left: ObservationDescriptor, right: ObservationDescriptor) -> bool:
    """Two party descriptors (e.g. a CDR record's caller and callee) sharing
    one origin observation are the two ends of the *same* event, not two
    independent signals about the same identity -- never a candidate
    merely for that reason. `descriptors_from_observation`'s two-party
    expansion is the only producer that can make this `True`; every other
    descriptor is already one-per-observation."""
    return left.observation_id == right.observation_id


def _tokens(item: ObservationDescriptor) -> tuple[str, ...]:
    values = (*item.aliases, *item.transliterations)
    return tuple(token.casefold() for value in values for token in _TOKEN.findall(value))


def normalise_identifier(identifier_type: str, value: str) -> str | None:
    """Comparison key for stable, source-backed identifier claims only."""
    if identifier_type not in _IDENTIFIER_TYPES:
        return None
    if identifier_type == "phone":
        digits = "".join(character for character in value if character.isdigit())
        return f"+{digits}" if len(digits) >= 7 else None
    if identifier_type == "email":
        normalized = value.strip().casefold()
        return normalized if "@" in normalized else None
    normalized = "".join(character for character in value.casefold() if character.isalnum())
    return normalized or None


def hashed_token_vector(item: ObservationDescriptor) -> tuple[float, ...]:
    vector = [0.0] * VECTOR_DIMENSIONS
    for token in _tokens(item):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        vector[int.from_bytes(digest[:4], "big") % VECTOR_DIMENSIONS] += 1.0
    length = math.sqrt(sum(value * value for value in vector))
    return tuple(value / length for value in vector) if length else tuple(vector)


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def retrieve_candidates(
    items: list[ObservationDescriptor], *, vector_threshold: float = 0.75
) -> tuple[RetrievedCandidate, ...]:
    """Return deterministic same-case candidates with named retrieval reasons."""
    _single_case(items)
    # Keyed by descriptor_id, not observation_id: two role-scoped
    # descriptors sharing one observation_id (a CDR record's caller and
    # callee) must accumulate independently here, or one role's match
    # would silently overwrite the other's under an identical
    # observation_id-based key.
    by_pair: dict[
        tuple[str, str],
        tuple[
            ObservationDescriptor,
            ObservationDescriptor,
            set[RetrievalReason],
            set[str],
            float | None,
            set[str],
        ],
    ] = {}
    vectors = {item.descriptor_id: hashed_token_vector(item) for item in items}
    for left, right in combinations(items, 2):
        if _same_origin_event(left, right):
            continue
        left, right = _ordered(left, right)
        reasons: set[RetrievalReason] = set()
        identifier_types: set[str] = set()
        contradictions: set[str] = set()
        for kind, value in left.identifiers.items():
            left_value = normalise_identifier(kind, value)
            right_value = normalise_identifier(kind, right.identifiers.get(kind, ""))
            if left_value is not None and left_value == right_value:
                reasons.add(RetrievalReason.EXACT_IDENTIFIER)
                identifier_types.add(kind)
            elif left_value is not None and right_value is not None:
                contradictions.add(f"conflicting_{kind}_claim")
        if (
            left.platform
            and left.platform == right.platform
            and set(left.handles) & set(right.handles)
        ):
            reasons.add(RetrievalReason.PLATFORM_HANDLE)
        if set(left.aliases) & set(right.aliases):
            reasons.add(RetrievalReason.EXACT_ALIAS)
        if {alias.casefold().strip() for alias in left.aliases} & {
            alias.casefold().strip() for alias in right.aliases
        }:
            reasons.add(RetrievalReason.NORMALIZED_ALIAS)
        # Symmetric in both directions: a transliteration candidate on
        # *either* side may match the other side's alias or transliteration
        # set. `_ordered` fixes which descriptor is "left" independently of
        # random observation-ID generation, so checking only
        # `left.transliterations` against `right`'s fields would make this
        # reason's presence depend on which of the two happened to sort
        # first -- a genuine, order-dependent bug, not an intentional
        # asymmetry (transliteration/alias overlap has no natural
        # direction).
        if (set(left.transliterations) & (set(right.aliases) | set(right.transliterations))) or (
            set(right.transliterations) & set(left.aliases)
        ):
            reasons.add(RetrievalReason.TRANSLITERATION)
        vector_score = _cosine(vectors[left.descriptor_id], vectors[right.descriptor_id])
        if vector_score >= vector_threshold and vector_score < 1.0:
            reasons.add(RetrievalReason.VECTOR)
        if not reasons:
            continue
        key = (str(left.descriptor_id), str(right.descriptor_id))
        by_pair[key] = (
            left,
            right,
            reasons,
            identifier_types,
            vector_score if RetrievalReason.VECTOR in reasons else None,
            contradictions,
        )
    if len(by_pair) > MAX_CANDIDATES:
        raise ValueError("candidate retrieval exceeded bounded limit")

    # Merge by the *observation*-pair a candidate is ultimately reported
    # against: two role-scoped descriptors of one two-party structured
    # record (e.g. a CDR record's caller and callee) that both
    # independently match the same other observation must still surface
    # as exactly one `RetrievedCandidate` for that observation pair --
    # `by_pair` above tracked them separately (by descriptor_id) only to
    # avoid one role's match silently overwriting the other's while
    # accumulating.
    merged: dict[
        tuple[str, str],
        tuple[
            ObservationDescriptor,
            ObservationDescriptor,
            set[RetrievalReason],
            set[str],
            float | None,
            set[str],
        ],
    ] = {}
    for (
        left,
        right,
        reason_set,
        identifier_types,
        stored_vector_score,
        contradiction_set,
    ) in by_pair.values():
        output_key = (str(left.observation_id), str(right.observation_id))
        existing = merged.get(output_key)
        if existing is None:
            merged[output_key] = (
                left,
                right,
                set(reason_set),
                set(identifier_types),
                stored_vector_score,
                set(contradiction_set),
            )
            continue
        (
            existing_left,
            existing_right,
            existing_reasons,
            existing_kinds,
            existing_score,
            existing_contradictions,
        ) = existing
        existing_reasons |= reason_set
        existing_kinds |= identifier_types
        existing_contradictions |= contradiction_set
        merged_score = (
            max(existing_score, stored_vector_score)
            if existing_score is not None and stored_vector_score is not None
            else existing_score
            if existing_score is not None
            else stored_vector_score
        )
        merged[output_key] = (
            existing_left,
            existing_right,
            existing_reasons,
            existing_kinds,
            merged_score,
            existing_contradictions,
        )

    results = []
    for (
        left,
        right,
        reason_set,
        identifier_types,
        stored_vector_score,
        contradiction_set,
    ) in merged.values():
        sorted_reasons = tuple(sorted(reason_set, key=str))
        results.append(
            RetrievedCandidate(
                case_id=left.case_id,
                left_observation_id=left.observation_id,
                right_observation_id=right.observation_id,
                reasons=sorted_reasons,
                identifier_types=tuple(sorted(identifier_types)),
                vector_score=stored_vector_score,
                supporting_observation_ids=(left.observation_id, right.observation_id),
                supporting_evidence_ids=(left.evidence_id, right.evidence_id),
                contradiction_reasons=tuple(sorted(contradiction_set)),
            )
        )
    return tuple(
        sorted(
            results,
            key=lambda item: (str(item.left_observation_id), str(item.right_observation_id)),
        )
    )
