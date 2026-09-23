"""Synthetic-only tests for Phase 5 reviewable intelligence behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import combinations
from uuid import uuid4

import pytest

from app.modules.graph.intelligence.analytics import (
    analyse,
    detect_communication_transfer_movement_motifs,
)
from app.modules.graph.intelligence.correlation import (
    add_temporal_hot_window_reason,
    build_correlation_submission,
)
from app.modules.graph.intelligence.models import (
    GraphEdgeSnapshot,
    ObservationDescriptor,
    RetrievalReason,
    RetrievedCandidate,
)
from app.modules.graph.intelligence.retrieval import exact_identifier_blocks, retrieve_candidates
from app.modules.graph.intelligence.scoring import score_candidates
from app.modules.graph.intelligence.vector_store import (
    VECTOR_DIMENSIONS,
    PgvectorCandidateStore,
    case_tfidf_vectors,
    source_snapshot_hash,
)


def _item(case_id, *, phone: str, alias: str, at: int = 0) -> ObservationDescriptor:
    observation_id = uuid4()
    return ObservationDescriptor(
        case_id=case_id,
        observation_id=observation_id,
        evidence_id=uuid4(),
        source_locator_reference=f"synthetic:{observation_id}",
        identifiers={"phone": phone},
        aliases=(alias,),
        transliterations=("raama",) if alias != "Rama" else (),
        event_start=datetime(2026, 1, 1, 12, at, tzinfo=UTC),
        event_end=datetime(2026, 1, 1, 12, at + 1, tzinfo=UTC),
    )


def test_exact_identifier_blocking_is_deterministic_case_scoped_and_candidate_only() -> None:
    case_id = uuid4()
    first, second = (
        _item(case_id, phone="+919999000001", alias="राम"),
        _item(case_id, phone="+919999000001", alias="Rama"),
    )
    candidates = retrieve_candidates([first, second])
    assert candidates == retrieve_candidates([second, first])
    assert candidates[0].reasons[0] is RetrievalReason.EXACT_IDENTIFIER
    scored = score_candidates(candidates)
    assert scored[0].status.value in {"candidate", "needs_review"}
    assert "never identity verification" in scored[0].explanation


def _repeated_identifier_items(case_id, *, count: int) -> list[ObservationDescriptor]:
    """`count` descriptors sharing one phone identifier, otherwise fixture-
    isolated so ONLY that identifier produces a match: no aliases (empty,
    not merely unique -- `_item`'s transliteration default of "raama" for
    any alias != "Rama" would make every synthetic item collide on
    TRANSLITERATION too), and identical empty-alias vectors give every pair
    a cosine of exactly 1.0, which `retrieve_candidates` already excludes
    from the VECTOR reason by design (a "trivially identical vector" is
    not a fuzzy match) -- avoiding the vector-similarity reason firing
    incidentally from shared substrings a differently-chosen fixture would
    introduce."""
    return [
        ObservationDescriptor(
            case_id=case_id,
            observation_id=uuid4(),
            evidence_id=uuid4(),
            source_locator_reference=f"synthetic:{i}",
            identifiers={"phone": "+919999000001"},
            aliases=(),
            transliterations=(),
            event_start=datetime(2026, 1, 1, 12, i, tzinfo=UTC),
            event_end=datetime(2026, 1, 1, 12, i + 1, tzinfo=UTC),
        )
        for i in range(count)
    ]


def test_exact_identifier_blocking_produces_zero_within_block_candidates_regardless_of_n() -> None:
    """ADR-029: true Tier-1 "exact blocking" (master plan Section 15.2)
    supersedes `identifier_star_edges` (ADR-027, still N-1 pairwise
    candidates within one group). Reproduces the real bug found against a
    live Fulcrum case (an identifier mentioned 14 times produced
    C(14,2)=91 candidate pairs restating the identical fact) at N=14 --
    without blocking, the naive scan still produces the full C(N,2)
    explosion (proving this reproduces the actual bug, not a strawman);
    with blocking, exactly zero within-block candidates are produced, not
    N-1. Also proven at N=250 (comfortably over MAX_CANDIDATES=200,
    reproducing Nightfall's real volume failure): the case must not crash
    regardless of N, since a block's own size never contributes to the
    candidate count at all once blocked."""
    case_id = uuid4()

    small = _repeated_identifier_items(case_id, count=14)
    naive = retrieve_candidates(small)
    assert len(naive) == 14 * 13 // 2  # C(14, 2) = 91 -- the bug, reproduced

    blocked = retrieve_candidates(small, exact_identifier_blocks=exact_identifier_blocks(small))
    assert blocked == ()  # zero within-block candidates, not N - 1 star edges

    large_case_id = uuid4()
    at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    large = [
        ObservationDescriptor(
            case_id=large_case_id,
            observation_id=uuid4(),
            evidence_id=uuid4(),
            source_locator_reference=f"synthetic:large:{i}",
            identifiers={"phone": "+919999000009"},
            aliases=(),
            transliterations=(),
            event_start=at,
            event_end=at,
        )
        for i in range(250)
    ]
    deduped_large = retrieve_candidates(
        large, exact_identifier_blocks=exact_identifier_blocks(large)
    )
    assert deduped_large == ()  # still zero -- MAX_CANDIDATES never even threatened


def test_exact_identifier_blocking_still_surfaces_a_real_cross_block_signal() -> None:
    """Blocking must only ever suppress *within-block* comparisons -- two
    descriptors in different blocks (different phones here) that share a
    real Tier 2-4 signal (an alias) must still produce a real, scoreable
    candidate, completely unaffected by blocking being active."""
    case_id = uuid4()
    first = _item(case_id, phone="+919999000001", alias="राम")
    second = _item(case_id, phone="+919999000002", alias="राम")
    naive = retrieve_candidates([first, second])
    blocked = retrieve_candidates(
        [first, second], exact_identifier_blocks=exact_identifier_blocks([first, second])
    )
    assert len(naive) == len(blocked) == 1
    assert RetrievalReason.EXACT_ALIAS in blocked[0].reasons
    assert RetrievalReason.EXACT_IDENTIFIER not in blocked[0].reasons


def test_exact_identifier_blocks_unions_transitively_across_different_identifier_kinds() -> None:
    """Strictly more correct than `identifier_star_edges`, which only ever
    grouped within one `(kind, value)` pair: a descriptor linked to A via a
    shared phone and to B via a shared account merges A and B into one
    component, and none of the three ever produce a candidate against each
    other."""
    case_id = uuid4()

    def descriptor(
        *, phone: str | None = None, account: str | None = None
    ) -> ObservationDescriptor:
        identifiers = {}
        if phone is not None:
            identifiers["phone"] = phone
        if account is not None:
            identifiers["account"] = account
        observation_id = uuid4()
        return ObservationDescriptor(
            case_id=case_id,
            observation_id=observation_id,
            evidence_id=uuid4(),
            source_locator_reference=f"synthetic:{observation_id}",
            identifiers=identifiers,
            aliases=(),
            transliterations=(),
            event_start=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            event_end=datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
        )

    bridge = descriptor(phone="+919999000005", account="ACC-1")
    via_phone = descriptor(phone="+919999000005")
    via_account = descriptor(account="ACC-1")
    items = [bridge, via_phone, via_account]

    blocks = exact_identifier_blocks(items)
    roots = {blocks[str(item.descriptor_id)] for item in items}
    assert len(roots) == 1  # all three collapsed into one component

    candidates = retrieve_candidates(items, exact_identifier_blocks=blocks)
    assert candidates == ()


def test_pipeline_call_site_never_opts_into_exact_identifier_blocking() -> None:
    """Phase 5's frozen, release-freeze-gated correlation pipeline
    (`pipeline.build_case_correlation_submission`) must keep calling
    `retrieve_candidates` with no `exact_identifier_blocks` argument -- its
    output must stay byte-for-byte unaffected by this change."""
    import inspect

    from app.modules.graph.intelligence import pipeline

    source = inspect.getsource(pipeline.build_case_correlation_submission)
    assert "exact_identifier_blocks" not in source
    assert "lexical_blocks" not in source
    assert "include_inline_vector" not in source


def _alias_only(case_id, *, alias: str, at: int = 0) -> ObservationDescriptor:
    observation_id = uuid4()
    return ObservationDescriptor(
        case_id=case_id,
        observation_id=observation_id,
        evidence_id=uuid4(),
        source_locator_reference=f"synthetic:{observation_id}",
        identifiers={},
        aliases=(alias,),
        transliterations=(),
        event_start=datetime(2026, 1, 1, 12, at, tzinfo=UTC),
        event_end=datetime(2026, 1, 1, 12, at + 1, tzinfo=UTC),
    )


def test_lexical_blocking_produces_zero_within_block_candidates_regardless_of_n() -> None:
    """ADR-030: true Tier-2 "normalized lexical blocking". Reproduces the
    real bug found live against Nightfall (10 alias values repeated 6
    times each -> 150 EXACT_ALIAS pairs) at the same N=6 scale, and at a
    larger N=250 to prove it can't crash MAX_CANDIDATES=200 either."""
    from app.modules.graph.intelligence.retrieval import lexical_blocks

    case_id = uuid4()
    small = [_alias_only(case_id, alias="SYN-PER-NF-01", at=i) for i in range(6)]
    naive = retrieve_candidates(small)
    assert len(naive) == 6 * 5 // 2  # C(6, 2) = 15 -- the bug, reproduced

    blocked = retrieve_candidates(small, lexical_blocks=lexical_blocks(small))
    assert blocked == ()

    large_case_id = uuid4()
    at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    large = [
        ObservationDescriptor(
            case_id=large_case_id,
            observation_id=uuid4(),
            evidence_id=uuid4(),
            source_locator_reference=f"synthetic:large:{i}",
            identifiers={},
            aliases=("SYN-PER-NF-01",),
            transliterations=(),
            event_start=at,
            event_end=at,
        )
        for i in range(250)
    ]
    deduped_large = retrieve_candidates(large, lexical_blocks=lexical_blocks(large))
    assert deduped_large == ()


def test_lexical_blocks_unions_alias_and_transliteration_and_handle_in_one_space() -> None:
    """One combined token space, not four separate reasons: a descriptor
    linked to A via a shared (casefolded) alias and to B via a shared
    (platform, handle) pair merges A and B into one component, exactly
    like `exact_identifier_blocks` unions across identifier kinds."""
    from app.modules.graph.intelligence.retrieval import lexical_blocks

    case_id = uuid4()

    def descriptor(*, alias: str | None = None, handle: str | None = None) -> ObservationDescriptor:
        observation_id = uuid4()
        return ObservationDescriptor(
            case_id=case_id,
            observation_id=observation_id,
            evidence_id=uuid4(),
            source_locator_reference=f"synthetic:{observation_id}",
            identifiers={},
            aliases=(alias,) if alias else (),
            transliterations=(),
            platform="telegram" if handle else None,
            handles=(handle,) if handle else (),
            event_start=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            event_end=datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
        )

    bridge = descriptor(alias="Rama", handle="rama_handle")
    via_alias = descriptor(alias="RAMA")  # NORMALIZED_ALIAS match (casefold)
    via_handle = descriptor(handle="rama_handle")
    items = [bridge, via_alias, via_handle]

    blocks = lexical_blocks(items)
    roots = {blocks[str(item.descriptor_id)] for item in items}
    assert len(roots) == 1  # all three collapsed into one component

    candidates = retrieve_candidates(items, lexical_blocks=blocks)
    assert candidates == ()


def test_lexical_blocking_still_surfaces_a_real_cross_block_signal() -> None:
    """Blocking must only suppress *within-block* comparisons -- two
    descriptors in different lexical blocks (different aliases) that
    share a real identifier signal must still produce a real candidate."""
    from app.modules.graph.intelligence.retrieval import lexical_blocks

    case_id = uuid4()

    def descriptor(*, alias: str) -> ObservationDescriptor:
        observation_id = uuid4()
        return ObservationDescriptor(
            case_id=case_id,
            observation_id=observation_id,
            evidence_id=uuid4(),
            source_locator_reference=f"synthetic:{observation_id}",
            identifiers={"phone": "+919999000001"},
            aliases=(alias,),
            transliterations=(),
            event_start=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            event_end=datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
        )

    first = descriptor(alias="राम")
    second = descriptor(alias="Nisha")
    naive = retrieve_candidates([first, second])
    blocked = retrieve_candidates([first, second], lexical_blocks=lexical_blocks([first, second]))
    assert len(naive) == len(blocked) == 1
    assert RetrievalReason.EXACT_IDENTIFIER in blocked[0].reasons


def test_merge_candidates_unions_reasons_for_the_same_observation_pair() -> None:
    from app.modules.graph.intelligence.retrieval import merge_candidates

    case_id = uuid4()
    first, second = (
        _item(case_id, phone="+919999000001", alias="A"),
        _item(case_id, phone="+919999000002", alias="B"),
    )
    tier_a = (
        RetrievedCandidate(
            case_id=case_id,
            left_observation_id=first.observation_id,
            right_observation_id=second.observation_id,
            reasons=(RetrievalReason.EXACT_ALIAS,),
            identifier_types=(),
            vector_score=None,
            supporting_observation_ids=(first.observation_id, second.observation_id),
            supporting_evidence_ids=(first.evidence_id, second.evidence_id),
            contradiction_reasons=(),
        ),
    )
    tier_b = (
        RetrievedCandidate(
            case_id=case_id,
            left_observation_id=first.observation_id,
            right_observation_id=second.observation_id,
            reasons=(RetrievalReason.VECTOR,),
            identifier_types=(),
            vector_score=0.9,
            supporting_observation_ids=(first.observation_id, second.observation_id),
            supporting_evidence_ids=(first.evidence_id, second.evidence_id),
            contradiction_reasons=(),
        ),
    )
    merged = merge_candidates(tier_a, tier_b)
    assert len(merged) == 1
    assert set(merged[0].reasons) == {RetrievalReason.EXACT_ALIAS, RetrievalReason.VECTOR}
    assert merged[0].vector_score == 0.9


def test_fulcrum_shaped_recall_is_zero_under_true_blocking_not_hand_tuned() -> None:
    """Regression test against Fulcrum's actual truth structure:
    `generate_entity_resolution_truth.py::build_pairs` asserts a "same"
    pair using the FIRST TWO entity UUIDs a token resolves to, in entity-
    listing order (`created_at DESC, entity_id DESC` -- effectively
    uncorrelated with `descriptor_id`, since `entity_id` is a hash of
    `observation_id`). Every one of Fulcrum's 12 truth "same" pairs is, by
    that script's own construction, two members of one identifier block.

    True blocking makes within-block candidates zero by design (see
    `test_exact_identifier_blocking_produces_zero_within_block_candidates_
    regardless_of_n` above) -- so for ANY truth-pair-selection order,
    independent of `descriptor_id`, recall against a Fulcrum-shaped truth
    set is provably, mathematically 0/N, not merely "not improved" over
    the prior `identifier_star_edges` baseline (2/12, live-measured). This
    is computed for real below, not asserted from the theory alone -- see
    ADR-029's "Known, accepted limitation" section for the live-measured
    Fulcrum dev/val numbers this predicts.
    """
    case_id = uuid4()
    # Mirrors Fulcrum's real shape: several identifier blocks of varying
    # size (most real identifiers repeat 2-5 times across CDR/finance/
    # social/sightings records).
    block_sizes = [2, 2, 2, 3, 3, 4, 2, 5, 2, 3, 2, 4]
    truth_pairs: list[tuple[str, str]] = []
    all_items: list[ObservationDescriptor] = []
    for index, size in enumerate(block_sizes):
        phone = f"+9199990{index:05d}"
        members = [
            ObservationDescriptor(
                case_id=case_id,
                observation_id=uuid4(),
                evidence_id=uuid4(),
                source_locator_reference=f"synthetic:{index}:{member}",
                identifiers={"phone": phone},
                aliases=(),
                transliterations=(),
                event_start=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
                event_end=datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
            )
            for member in range(size)
        ]
        all_items.extend(members)
        # `build_pairs` picks the first two entities in an order unrelated
        # to descriptor_id -- insertion order here plays that role.
        truth_pairs.append((str(members[0].observation_id), str(members[1].observation_id)))

    blocks = exact_identifier_blocks(all_items)
    candidates = retrieve_candidates(all_items, exact_identifier_blocks=blocks)

    found_observation_pairs = {
        frozenset({str(c.left_observation_id), str(c.right_observation_id)}) for c in candidates
    }
    hits = sum(1 for pair in truth_pairs if frozenset(pair) in found_observation_pairs)
    assert candidates == ()  # true blocking: no within-block candidate exists at all
    assert hits == 0  # real computed recall against this truth structure: 0/12, not hand-tuned


def test_cross_case_retrieval_is_rejected() -> None:
    with pytest.raises(ValueError, match="multiple cases"):
        retrieve_candidates(
            [
                _item(uuid4(), phone="+919999000001", alias="A"),
                _item(uuid4(), phone="+919999000001", alias="A"),
            ]
        )


def test_local_vector_snapshot_is_deterministic_and_case_bound() -> None:
    case_id = uuid4()
    item = _item(case_id, phone="+919999000009", alias="निशा")
    assert source_snapshot_hash(item) == source_snapshot_hash(item)
    changed_case = item.model_copy(update={"case_id": uuid4()})
    assert source_snapshot_hash(item) != source_snapshot_hash(changed_case)


def _alias_only_item(case_id, *, alias: str) -> ObservationDescriptor:
    observation_id = uuid4()
    return ObservationDescriptor(
        case_id=case_id,
        observation_id=observation_id,
        evidence_id=uuid4(),
        source_locator_reference=f"synthetic:{observation_id}",
        identifiers={},
        aliases=(alias,),
        transliterations=(),
        event_start=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        event_end=datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
    )


def test_case_tfidf_vectors_discriminates_structurally_similar_short_names() -> None:
    """ADR-032: reproduces Nightfall's real live-confirmed failure mode --
    10 structurally near-identical short synthetic names (differing only
    in a trailing sequence number) collapsed into one 43/45-edge clique
    under `hashed_token_vector`'s coarse 32-bucket hash. Character n-gram
    TF-IDF + SVD must give each name its own distinguishable vector, not a
    uniform near-1.0 similarity across every pair."""
    case_id = uuid4()
    items = [_alias_only_item(case_id, alias=f"SYN-PER-NF-{i:02d}") for i in range(1, 11)]

    vectors = case_tfidf_vectors(items)

    assert len(vectors) == 10
    assert all(len(vector) == VECTOR_DIMENSIONS for vector in vectors.values())
    similarities = [
        sum(
            a * b
            for a, b in zip(vectors[left.descriptor_id], vectors[right.descriptor_id], strict=True)
        )
        for left, right in combinations(items, 2)
    ]
    # Real, computed spread -- never all pairs landing near 1.0 the way
    # the old hash scheme's collisions produced.
    assert max(similarities) < 0.9
    assert len({round(value, 6) for value in similarities}) > 1


def test_case_tfidf_vectors_is_deterministic() -> None:
    case_id = uuid4()
    items = [_alias_only_item(case_id, alias=f"SYN-PER-NF-{i:02d}") for i in range(1, 8)]
    assert case_tfidf_vectors(items) == case_tfidf_vectors(items)


def test_case_tfidf_vectors_handles_empty_and_degenerate_input_without_crashing() -> None:
    assert case_tfidf_vectors([]) == {}

    case_id = uuid4()
    blank = ObservationDescriptor(
        case_id=case_id,
        observation_id=uuid4(),
        evidence_id=uuid4(),
        source_locator_reference="synthetic:blank",
        identifiers={},
        aliases=(),
        transliterations=(),
        event_start=None,
        event_end=None,
    )
    vectors = case_tfidf_vectors([blank])
    assert len(vectors[blank.descriptor_id]) == VECTOR_DIMENSIONS
    assert all(value == 0.0 for value in vectors[blank.descriptor_id])

    single = _alias_only_item(case_id, alias="lone-descriptor")
    single_vectors = case_tfidf_vectors([single])
    assert len(single_vectors[single.descriptor_id]) == VECTOR_DIMENSIONS


def test_case_tfidf_vectors_never_crosses_case_boundaries_in_its_own_fit() -> None:
    """Two separate calls (one per case) must never let one case's
    vocabulary influence another's -- `case_tfidf_vectors` is fit fresh
    every call, never a shared/cached vectorizer."""
    alias = "SYN-PER-SHARED-01"
    first_case_items = [_alias_only_item(uuid4(), alias=alias)]
    second_case_items = [_alias_only_item(uuid4(), alias=alias)]
    first_vectors = case_tfidf_vectors(first_case_items)
    second_vectors = case_tfidf_vectors(second_case_items)
    assert (
        first_vectors[first_case_items[0].descriptor_id]
        == second_vectors[second_case_items[0].descriptor_id]
    )


class _VectorResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self._rows


class _VectorConnection:
    def __init__(self) -> None:
        self.statement = None
        self.parameters = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def execute(self, statement, parameters):
        self.statement = statement
        self.parameters = parameters
        return _VectorResult([])


class _VectorEngine:
    def __init__(self) -> None:
        self.connection = _VectorConnection()

    def connect(self):
        return self.connection


async def test_pgvector_search_binds_the_descriptor_case_before_returning_candidates() -> None:
    descriptor = _item(uuid4(), phone="+919999000009", alias="Nisha")
    engine = _VectorEngine()

    results = await PgvectorCandidateStore(engine).search(descriptor)  # type: ignore[arg-type]

    assert results == []
    assert "WHERE case_id = :case_id" in str(engine.connection.statement)
    assert engine.connection.parameters["case_id"] == descriptor.case_id
    assert engine.connection.parameters["observation_id"] == descriptor.observation_id


def test_conflicting_source_backed_identifier_is_retained_as_a_contradiction() -> None:
    case_id = uuid4()
    first = _item(case_id, phone="+919999000007", alias="Same Alias")
    second = _item(case_id, phone="+919999000008", alias="Same Alias")
    candidate = retrieve_candidates([first, second])[0]
    assert candidate.contradiction_reasons == ("conflicting_phone_claim",)
    score = score_candidates((candidate,))[0]
    assert any(item.feature == "contradiction" for item in score.contributions)


def test_submission_uses_nipun_candidate_only_seam_with_reproducible_snapshot() -> None:
    case_id = uuid4()
    scored = score_candidates(
        retrieve_candidates(
            [
                _item(case_id, phone="+919999000003", alias="राम"),
                _item(case_id, phone="+919999000003", alias="Rama"),
            ]
        )
    )
    submission = build_correlation_submission(scored)
    assert submission.status.value == "needs_review"
    assert submission.feature_snapshot is not None
    assert submission.feature_snapshot.values["candidate_only"] is True


def test_temporal_reason_requires_bounded_same_case_windows() -> None:
    case_id = uuid4()
    first, second = (
        _item(case_id, phone="+919999000002", alias="A", at=0),
        _item(case_id, phone="+919999000002", alias="B", at=2),
    )
    retrieved = retrieve_candidates([first, second])
    temporal = add_temporal_hot_window_reason(retrieved, (first, second))
    assert RetrievalReason.TEMPORAL in temporal[0].reasons
    no_time = first.model_copy(update={"event_start": None, "event_end": None})
    assert (
        RetrievalReason.TEMPORAL
        not in add_temporal_hot_window_reason(retrieved, (no_time, second))[0].reasons
    )


def test_analytics_are_case_scoped_and_deterministic_for_a_snapshot() -> None:
    case_id = uuid4()
    first_id, second_id, third_id = uuid4(), uuid4(), uuid4()
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    edges = (
        GraphEdgeSnapshot(
            case_id=case_id,
            left_id=first_id,
            right_id=second_id,
            event_id=uuid4(),
            evidence_observation_ids=(uuid4(),),
            event_kind="cdr_call",
            event_start=base,
            event_end=base.replace(minute=1),
        ),
        GraphEdgeSnapshot(
            case_id=case_id,
            left_id=second_id,
            right_id=third_id,
            event_id=uuid4(),
            evidence_observation_ids=(uuid4(),),
            event_kind="financial_transaction",
            event_start=base.replace(minute=2),
            event_end=base.replace(minute=3),
        ),
        GraphEdgeSnapshot(
            case_id=case_id,
            left_id=third_id,
            right_id=uuid4(),
            event_id=uuid4(),
            evidence_observation_ids=(uuid4(),),
            event_kind="movement",
            event_start=base.replace(minute=4),
            event_end=base.replace(minute=5),
        ),
    )
    first, second = analyse(edges), analyse(edges)
    assert first[0].graph_snapshot_hash == second[0].graph_snapshot_hash
    assert first[0].values == second[0].values
    assert any(key.startswith("leiden:") for key in first[0].values)
    assert any(key.startswith("betweenness:") for key in first[0].values)
    motif = detect_communication_transfer_movement_motifs(edges)
    assert motif and motif[0].supporting_observation_ids
