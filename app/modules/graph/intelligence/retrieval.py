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


def exact_identifier_blocks(items: list[ObservationDescriptor]) -> dict[str, str]:
    """ADR-029: true Tier-1 "exact blocking" (master plan Section 15.2) --
    supersedes the entity-resolution-cascade-v2 `identifier_star_edges`
    approach (ADR-027), which still emitted `N-1` pairwise candidate rows
    *within* one identifier's group. Blocking is pure grouping, not
    candidate generation: descriptors sharing an exact identifier value are
    never compared against each other at all (no reason computed, no
    candidate emitted) -- Tiers 2-4 only ever run *between* distinct
    blocks, via `retrieve_candidates`'s `exact_identifier_blocks` param.

    Union-find over descriptor IDs, transitive and cross-kind: a
    descriptor linked to block A via a shared phone and to block B via a
    shared account merges A and B into one component -- `identifier_star_
    edges` only ever grouped within one `(kind, value)` pair, never across
    kinds, so this is strictly more correct blocking, not just a rename.

    Returns `{descriptor_id: canonical_descriptor_id}` for every descriptor
    that belongs to a block of size >= 2 -- `canonical_descriptor_id` is
    the lexicographically-smallest descriptor_id in that final component,
    deterministic regardless of union order. A descriptor with no
    identifier that repeats anywhere in `items` (or no Tier-1 identifier
    at all) has no entry here -- it is never "blocked" with anyone, and
    `retrieve_candidates` treats it as its own singleton, compared against
    every other item exactly as before blocking existed.

    Only `retrieve_candidates`'s caller opts into this -- passing it as
    `exact_identifier_blocks` -- never applied unless explicitly requested,
    so every existing call site's output (Phase 5's frozen correlation
    pipeline included) is unaffected.
    """
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        if left_root < right_root:
            parent[right_root] = left_root
        else:
            parent[left_root] = right_root

    by_value: dict[tuple[str, str], list[str]] = {}
    for item in items:
        descriptor_id = str(item.descriptor_id)
        parent.setdefault(descriptor_id, descriptor_id)
        for kind, raw in item.identifiers.items():
            normalized = normalise_identifier(kind, raw)
            if normalized is None:
                continue
            by_value.setdefault((kind, normalized), []).append(descriptor_id)

    for members in by_value.values():
        if len(members) < 2:
            continue
        first = members[0]
        for other in members[1:]:
            union(first, other)

    components: dict[str, list[str]] = {}
    for descriptor_id in parent:
        components.setdefault(find(descriptor_id), []).append(descriptor_id)

    blocks: dict[str, str] = {}
    for members in components.values():
        if len(members) < 2:
            continue
        canonical = min(members)
        for member in members:
            blocks[member] = canonical
    return blocks


def _casefold_tokens(item: ObservationDescriptor) -> tuple[str, ...]:
    """Every normalized lexical token a descriptor carries -- aliases and
    transliterations, casefolded and stripped identically to
    `RetrievalReason.NORMALIZED_ALIAS`'s own comparison, so a value that
    would have matched under that reason is guaranteed to land in the same
    `lexical_blocks` component."""
    return tuple(
        value.casefold().strip()
        for value in (*item.aliases, *item.transliterations)
        if value.strip()
    )


def _handle_tokens(item: ObservationDescriptor) -> tuple[str, ...]:
    if not item.platform:
        return ()
    return tuple(f"{item.platform}\x00{handle}" for handle in item.handles)


def lexical_blocks(items: list[ObservationDescriptor]) -> dict[str, str]:
    """ADR-030: true Tier-2 "normalized lexical blocking" (master plan
    Section 15.2), same shape as `exact_identifier_blocks` (Tier 1, ADR-029)
    applied to a different signal: descriptors sharing a normalized alias/
    transliteration token, or a `(platform, handle)` pair, are never
    compared against each other at all -- not scored, not emitted as a
    candidate. `RetrievalReason.PLATFORM_HANDLE`/`EXACT_ALIAS`/
    `NORMALIZED_ALIAS`/`TRANSLITERATION`'s own checks in `retrieve_
    candidates` are unchanged code -- they simply become unreachable
    between two block representatives, exactly like `EXACT_IDENTIFIER`
    did under Tier-1 blocking, for the identical reason: two descriptors
    that would have matched on any of those checks share a token in this
    function's combined normalized-token space, so they are already the
    same block's one surviving member by construction.

    One combined token space (not four separate ones): `EXACT_ALIAS` is
    exactly `NORMALIZED_ALIAS` restricted to un-casefolded equality, and
    `TRANSLITERATION` already compares transliterations against the
    *other* side's aliases-or-transliterations -- blocking on
    `casefolded(alias) | casefolded(transliteration)` as one set
    subsumes all three lexical-name reasons in a single union-find, the
    same way `exact_identifier_blocks` unions across different
    *identifier kinds* rather than keeping phone/account/vehicle
    separate. Handle tokens are kept in the same union-find but
    namespaced (`platform\\x00handle`) so a handle string can never
    accidentally collide with an unrelated alias string.

    Union-find over descriptor IDs, transitive: a descriptor linked to
    block A via a shared alias and to block B via a shared handle merges
    A and B into one component. Returns `{descriptor_id:
    canonical_descriptor_id}` for every descriptor in a block of size
    >= 2, `canonical_descriptor_id` the lexicographically-smallest
    descriptor_id in the final component -- identical determinism
    contract to `exact_identifier_blocks`.

    Only `retrieve_candidates`'s caller opts into this -- passing it as
    `lexical_blocks` -- never applied unless explicitly requested, so
    every existing call site's output (Phase 5's frozen correlation
    pipeline included) is unaffected.
    """
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        if left_root < right_root:
            parent[right_root] = left_root
        else:
            parent[left_root] = right_root

    by_value: dict[str, list[str]] = {}
    for item in items:
        descriptor_id = str(item.descriptor_id)
        parent.setdefault(descriptor_id, descriptor_id)
        for token in (*_casefold_tokens(item), *_handle_tokens(item)):
            by_value.setdefault(token, []).append(descriptor_id)

    for members in by_value.values():
        if len(members) < 2:
            continue
        first = members[0]
        for other in members[1:]:
            union(first, other)

    components: dict[str, list[str]] = {}
    for descriptor_id in parent:
        components.setdefault(find(descriptor_id), []).append(descriptor_id)

    blocks: dict[str, str] = {}
    for members in components.values():
        if len(members) < 2:
            continue
        canonical = min(members)
        for member in members:
            blocks[member] = canonical
    return blocks


def apply_blocks(
    items: list[ObservationDescriptor], blocks: dict[str, str]
) -> list[ObservationDescriptor]:
    """Collapse `items` to just each block's one canonical representative
    (plus every unblocked singleton), per a `exact_identifier_blocks`/
    `lexical_blocks`-shaped mapping. `retrieve_candidates` uses this
    internally for Tiers 1-2; exposed so a caller composing Tier 3
    (`vector_store.vector_linked_candidates`) on the *same* reduced set --
    not the raw, unblocked item list -- can reuse the identical reduction
    rather than re-deriving it."""
    return [
        item
        for item in items
        if blocks.get(str(item.descriptor_id), str(item.descriptor_id)) == str(item.descriptor_id)
    ]


def merge_candidates(
    *candidate_groups: tuple[RetrievedCandidate, ...],
) -> tuple[RetrievedCandidate, ...]:
    """Combine several independently-produced candidate groups (e.g.
    Tiers 1-2's output and Tier 3's separately-queried pgvector output)
    into one deterministic set, merging by observation pair exactly like
    `retrieve_candidates`'s own internal merge: reasons/identifier_types/
    contradiction_reasons are unioned, `vector_score` takes the max of
    whichever groups reported one. Never mutates its inputs."""
    merged: dict[tuple[str, str], RetrievedCandidate] = {}
    for group in candidate_groups:
        for candidate in group:
            key = (str(candidate.left_observation_id), str(candidate.right_observation_id))
            existing = merged.get(key)
            if existing is None:
                merged[key] = candidate
                continue
            existing_score = existing.vector_score
            new_score = candidate.vector_score
            merged_score = (
                max(existing_score, new_score)
                if existing_score is not None and new_score is not None
                else existing_score
                if existing_score is not None
                else new_score
            )
            merged[key] = existing.model_copy(
                update={
                    "reasons": tuple(sorted({*existing.reasons, *candidate.reasons}, key=str)),
                    "identifier_types": tuple(
                        sorted({*existing.identifier_types, *candidate.identifier_types})
                    ),
                    "vector_score": merged_score,
                    "contradiction_reasons": tuple(
                        sorted({*existing.contradiction_reasons, *candidate.contradiction_reasons})
                    ),
                }
            )
    return tuple(
        sorted(
            merged.values(),
            key=lambda item: (str(item.left_observation_id), str(item.right_observation_id)),
        )
    )


def retrieve_candidates(
    items: list[ObservationDescriptor],
    *,
    vector_threshold: float = 0.75,
    exact_identifier_blocks: dict[str, str] | None = None,
    lexical_blocks: dict[str, str] | None = None,
    include_inline_vector: bool = True,
) -> tuple[RetrievedCandidate, ...]:
    """Return deterministic same-case candidates with named retrieval reasons.

    `exact_identifier_blocks` (Tier 1, ADR-029), `lexical_blocks` (Tier 2,
    ADR-030), and `include_inline_vector=False` (Tier 3 opt-out, ADR-030)
    are each `None`/`True` by default -- every existing call site's output
    is byte-for-byte unchanged, including Phase 5's frozen, release-freeze-
    gated `pipeline.build_case_correlation_submission`, which never passes
    any of them.

    When a caller passes `exact_identifier_blocks` and/or `lexical_blocks`,
    every descriptor belonging to a block collapses to just its one
    canonical member before any pairwise comparison happens, applied in
    sequence (Tier 1 first, Tier 2 further reduces whatever Tier 1 left) --
    true blocking (master plan Section 15.2): descriptors sharing an exact
    identifier or a normalized lexical token are never compared against
    each other at all, not scored, not emitted as a candidate.
    `RetrievalReason.EXACT_IDENTIFIER`/`PLATFORM_HANDLE`/`EXACT_ALIAS`/
    `NORMALIZED_ALIAS`/`TRANSLITERATION`'s own checks are all unchanged
    code -- they simply become unreachable between two block
    representatives, since two descriptors that would have matched are
    already the same block's one surviving member by construction.

    `include_inline_vector=False` (Tier 3) retires the all-pairs cosine
    scan entirely for this call -- pair with `vector_store.
    vector_linked_candidates` (a genuine bounded pgvector nearest-neighbor
    query per descriptor, not a pairwise scan) and `merge_candidates` to
    recombine. `True` (default) preserves the exact legacy inline
    computation for every caller that doesn't opt out.

    `MAX_CANDIDATES` itself is unaffected by any of this -- every
    reduction happens to the input, never to the ceiling.
    """
    _single_case(items)
    if exact_identifier_blocks is not None:
        items = apply_blocks(items, exact_identifier_blocks)
    if lexical_blocks is not None:
        items = apply_blocks(items, lexical_blocks)
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
    vectors = (
        {item.descriptor_id: hashed_token_vector(item) for item in items}
        if include_inline_vector
        else {}
    )
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
        vector_score = None
        if include_inline_vector:
            vector_score = _cosine(vectors[left.descriptor_id], vectors[right.descriptor_id])
            if vector_score >= vector_threshold and vector_score < 1.0:
                reasons.add(RetrievalReason.VECTOR)
            else:
                vector_score = None
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
