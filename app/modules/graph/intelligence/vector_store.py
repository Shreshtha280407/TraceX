"""Optional PostgreSQL pgvector persistence and bounded same-case retrieval."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.canonical import canonical_sha256
from app.core.ids import deterministic_uuid
from app.modules.graph.intelligence.models import (
    ObservationDescriptor,
    RetrievalReason,
    RetrievedCandidate,
)
from app.modules.graph.intelligence.retrieval import (
    VECTOR_DIMENSIONS,
    VECTOR_PROVIDER,
    VECTOR_PROVIDER_VERSION,
    _ordered,
    _same_origin_event,
    hashed_token_vector,
)

VECTOR_CONFIGURATION_VERSION = "phase5_local_vector_retrieval_v1"

#: ADR-032: the real, case-scoped Tier-3 signal `vector_linked_candidates`
#: (below) uses -- character n-gram TF-IDF, reduced by `TruncatedSVD` to
#: `VECTOR_DIMENSIONS`, fit fresh per case (never persisted, never reused
#: across cases). Distinct provider identity from `VECTOR_PROVIDER`
#: (`retrieval.hashed_token_vector`'s single-token deterministic hash,
#: still used unchanged by every caller that doesn't opt into real Tier-3
#: retrieval -- Phase 5's frozen correlation pipeline included) so the two
#: are never conflated in `graph_intelligence_vectors`' own `provider`
#: column.
TFIDF_VECTOR_PROVIDER = "case_tfidf_char_ngram_svd"
TFIDF_VECTOR_PROVIDER_VERSION = "v1"


class VectorSearchUnavailable(RuntimeError):
    """The optional extension is absent or unavailable; callers can safely skip it."""


def _literal(vector: tuple[float, ...]) -> str:
    if len(vector) != VECTOR_DIMENSIONS:
        raise ValueError("vector dimension does not match configured pgvector column")
    return "[" + ",".join(format(value, ".12g") for value in vector) + "]"


def source_snapshot_hash(descriptor: ObservationDescriptor) -> str:
    return canonical_sha256(
        {
            "case_id": str(descriptor.case_id),
            "observation_id": str(descriptor.observation_id),
            "evidence_id": str(descriptor.evidence_id),
            "locator": descriptor.source_locator_reference,
            "aliases": descriptor.aliases,
            "transliterations": descriptor.transliterations,
            "provider": VECTOR_PROVIDER,
            "provider_version": VECTOR_PROVIDER_VERSION,
            "configuration_version": VECTOR_CONFIGURATION_VERSION,
        }
    )


def tfidf_snapshot_hash(descriptor: ObservationDescriptor) -> str:
    """ADR-032's own provenance hash, parallel to `source_snapshot_hash`
    but stamped with `TFIDF_VECTOR_PROVIDER`/`_VERSION` -- kept as a
    separate function rather than a parameter on `source_snapshot_hash`
    so that function's existing, tested behavior (and its own regression
    test, `test_local_vector_snapshot_is_deterministic_and_case_bound`)
    stays completely unchanged."""
    return canonical_sha256(
        {
            "case_id": str(descriptor.case_id),
            "observation_id": str(descriptor.observation_id),
            "evidence_id": str(descriptor.evidence_id),
            "locator": descriptor.source_locator_reference,
            "aliases": descriptor.aliases,
            "transliterations": descriptor.transliterations,
            "identifiers": descriptor.identifiers,
            "handles": descriptor.handles,
            "platform": descriptor.platform,
            "provider": TFIDF_VECTOR_PROVIDER,
            "provider_version": TFIDF_VECTOR_PROVIDER_VERSION,
            "configuration_version": VECTOR_CONFIGURATION_VERSION,
        }
    )


def _descriptor_text(item: ObservationDescriptor) -> str:
    """ADR-032: every textual field a descriptor carries, not just aliases/
    transliterations (`retrieval._tokens`'s own narrower scope) -- the full
    available context a case-scoped TF-IDF fit can use to discriminate
    between structurally similar short names, which a single-token
    deterministic hash cannot. `participant_role` (`"caller"`/`"callee"`/
    etc.) is deliberately excluded: it is evidence-local role metadata, not
    identity text, and including it would spuriously make every caller-role
    descriptor across unrelated people look more alike."""
    parts = [
        *item.aliases,
        *item.transliterations,
        *item.identifiers.values(),
        *item.handles,
    ]
    if item.platform:
        parts.append(item.platform)
    return " ".join(parts)


def case_tfidf_vectors(
    items: list[ObservationDescriptor],
) -> dict[UUID, tuple[float, ...]]:
    """ADR-032: real Tier-3 similarity signal -- character n-gram TF-IDF
    (`analyzer="char_wb"`, `ngram_range=(2, 4)`) fit fresh over exactly
    this case's own descriptor text (never cross-case, never persisted
    across calls), reduced by `TruncatedSVD` to `VECTOR_DIMENSIONS` so the
    output slots into the existing `graph_intelligence_vectors.embedding
    vector(32)` column unchanged -- no migration needed. Replaces
    `retrieval.hashed_token_vector`'s single deterministic hash-bucket
    scheme for this one call path only; every other caller (Tiers 1-2's
    own inline fallback, Phase 5's frozen correlation pipeline) keeps using
    `hashed_token_vector` completely unchanged.

    Why this fixes what the hash-bucket scheme could not: character
    n-grams capture exactly the subword differences (e.g. the trailing
    "01" vs "02" in two otherwise-identical short synthetic names) that a
    32-bucket modular hash washes out once a case's vocabulary is small --
    real Nightfall data live-confirmed this collapsed 10 structurally
    similar names into one 43/45-edge clique under the old scheme.

    Deterministic (`random_state=0`, matching this codebase's fixed-seed
    convention for `analytics.py`'s own Leiden modularity). Degenerate
    inputs (no items, every descriptor's text empty, or a corpus too small
    for `TruncatedSVD`'s `n_components < n_features` constraint) fall back
    to an all-zero vector per descriptor -- never a crash, never a
    fabricated non-zero signal from nothing.
    """
    zero_vector = tuple(0.0 for _ in range(VECTOR_DIMENSIONS))
    if not items:
        return {}
    texts = [_descriptor_text(item) for item in items]
    if not any(text.strip() for text in texts):
        return {item.descriptor_id: zero_vector for item in items}

    vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=1)
    try:
        matrix = vectorizer.fit_transform(texts)
    except ValueError:
        # Empty vocabulary -- every text was pure whitespace/punctuation
        # with no char_wb n-grams at all. Genuinely nothing to embed.
        return {item.descriptor_id: zero_vector for item in items}

    n_samples, n_features = matrix.shape

    def _normalized(values: list[float]) -> tuple[float, ...]:
        padded = values[:VECTOR_DIMENSIONS] + [0.0] * max(0, VECTOR_DIMENSIONS - len(values))
        length = math.sqrt(sum(value * value for value in padded))
        return tuple(value / length for value in padded) if length else tuple(padded)

    n_components = min(VECTOR_DIMENSIONS, n_features - 1, n_samples - 1)
    if n_components < 1:
        # Too small a corpus/vocabulary for a meaningful SVD reduction --
        # fall back to each descriptor's own raw (dense, padded/truncated,
        # normalized) TF-IDF row rather than crash on a degenerate call.
        dense = matrix.toarray()
        return {
            item.descriptor_id: _normalized(list(row))
            for item, row in zip(items, dense, strict=True)
        }

    svd = TruncatedSVD(n_components=n_components, random_state=0)
    reduced = svd.fit_transform(matrix)
    return {
        item.descriptor_id: _normalized(list(row)) for item, row in zip(items, reduced, strict=True)
    }


class PgvectorCandidateStore:
    """Stores deterministic local vectors; only PostgreSQL is contacted here."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def upsert(
        self,
        descriptor: ObservationDescriptor,
        *,
        now: datetime | None = None,
        embedding: tuple[float, ...] | None = None,
        provider: str | None = None,
        provider_version: str | None = None,
        snapshot_hash: str | None = None,
    ) -> UUID:
        """`embedding`/`provider`/`provider_version`/`snapshot_hash` default
        to the original `hashed_token_vector`/`VECTOR_PROVIDER`/`VECTOR_
        PROVIDER_VERSION`/`source_snapshot_hash` combination -- every
        pre-existing caller's behavior is byte-for-byte unchanged.
        `vector_linked_candidates` (ADR-032) is the one caller that passes
        all four explicitly, for the real case-scoped TF-IDF signal."""
        now = now or datetime.now(UTC)
        resolved_embedding = embedding if embedding is not None else hashed_token_vector(descriptor)
        resolved_provider = provider if provider is not None else VECTOR_PROVIDER
        resolved_provider_version = (
            provider_version if provider_version is not None else VECTOR_PROVIDER_VERSION
        )
        resolved_snapshot_hash = (
            snapshot_hash if snapshot_hash is not None else source_snapshot_hash(descriptor)
        )
        vector_id = deterministic_uuid(
            "phase5_pgvector_snapshot", str(descriptor.case_id), str(descriptor.observation_id)
        )
        statement = sa.text(
            "INSERT INTO graph_intelligence_vectors ("
            "vector_id, case_id, observation_id, provider, provider_version, configuration_hash, "
            "source_snapshot_hash, embedding, created_at) VALUES ("
            ":vector_id, :case_id, :observation_id, :provider, :provider_version, "
            ":configuration_hash, "
            ":source_snapshot_hash, CAST(:embedding AS vector), :created_at) "
            "ON CONFLICT (case_id, observation_id) DO UPDATE SET "
            "provider = EXCLUDED.provider, provider_version = EXCLUDED.provider_version, "
            "configuration_hash = EXCLUDED.configuration_hash, "
            "source_snapshot_hash = EXCLUDED.source_snapshot_hash, embedding = EXCLUDED.embedding"
        )
        try:
            async with self._engine.begin() as connection:
                await connection.execute(
                    statement,
                    {
                        "vector_id": vector_id,
                        "case_id": descriptor.case_id,
                        "observation_id": descriptor.observation_id,
                        "provider": resolved_provider,
                        "provider_version": resolved_provider_version,
                        "configuration_hash": canonical_sha256(
                            {
                                "version": VECTOR_CONFIGURATION_VERSION,
                                "dimensions": VECTOR_DIMENSIONS,
                            }
                        ),
                        "source_snapshot_hash": resolved_snapshot_hash,
                        "embedding": _literal(resolved_embedding),
                        "created_at": now,
                    },
                )
        except sa.exc.DBAPIError as exc:
            raise VectorSearchUnavailable("pgvector candidate search is unavailable") from exc
        return vector_id

    async def search(
        self,
        descriptor: ObservationDescriptor,
        *,
        limit: int = 20,
        embedding: tuple[float, ...] | None = None,
    ) -> list[tuple[UUID, float]]:
        """`embedding` defaults to `hashed_token_vector(descriptor)` --
        unchanged for every pre-existing caller. `vector_linked_candidates`
        passes the same TF-IDF vector it upserted for this descriptor, so
        the query and stored vectors are always in the same embedding
        space."""
        if not 1 <= limit <= 200:
            raise ValueError("pgvector candidate limit must be between 1 and 200")
        resolved_embedding = embedding if embedding is not None else hashed_token_vector(descriptor)
        statement = sa.text(
            "SELECT observation_id, 1 - (embedding <=> CAST(:embedding AS vector)) AS score "
            "FROM graph_intelligence_vectors WHERE case_id = :case_id "
            "AND observation_id <> :observation_id "
            "ORDER BY embedding <=> CAST(:embedding AS vector), "
            "observation_id LIMIT :limit"
        )
        try:
            async with self._engine.connect() as connection:
                rows = (
                    await connection.execute(
                        statement,
                        {
                            "case_id": descriptor.case_id,
                            "observation_id": descriptor.observation_id,
                            "embedding": _literal(resolved_embedding),
                            "limit": limit,
                        },
                    )
                ).mappings()
        except sa.exc.DBAPIError as exc:
            raise VectorSearchUnavailable("pgvector candidate search is unavailable") from exc
        return [(UUID(str(row["observation_id"])), float(row["score"])) for row in rows]


async def vector_linked_candidates(
    store: PgvectorCandidateStore,
    items: list[ObservationDescriptor],
    *,
    threshold: float = 0.75,
    limit: int = 20,
    now: datetime | None = None,
) -> tuple[RetrievedCandidate, ...]:
    """ADR-030: true Tier-3 "case-scoped pgvector retrieval" (master plan
    Section 15.2) -- replaces `retrieval.retrieve_candidates`'s inline,
    all-pairs cosine-similarity scan (an O(N^2) pairwise comparison over
    the *entire* item set, the same unbounded shape Tiers 1-2 had before
    their own true-blocking fixes) with a genuine bounded nearest-neighbor
    query per descriptor -- never more than `limit` candidates considered
    for any one descriptor, matching Tier 1/2's "retrieval, not pairwise"
    shape.

    Persists (upserts, idempotent) every descriptor's vector first, then
    queries each one's nearest same-case neighbors via `PgvectorCandidateStore.
    search` -- a real indexed `ORDER BY embedding <=> ... LIMIT` query, not
    a scan. Two descriptors sharing one `observation_id` (a two-party CDR/
    finance record's caller and callee) are excluded from their own search
    results by the store's own `observation_id <> :observation_id` clause,
    mirroring `retrieval._same_origin_event`'s exclusion for the identical
    reason -- still re-checked explicitly below for descriptors that
    resolve to the same observation via two different callers' distinct
    result rows.

    Caller composes this with `retrieval.retrieve_candidates(items,
    include_inline_vector=False, ...)` and `retrieval.merge_candidates` to
    recombine Tier 1-2's output with this tier's -- never called on its
    own to produce a complete candidate set.

    ADR-032: the vector persisted and queried here is `case_tfidf_vectors`'
    real, case-scoped TF-IDF+SVD signal -- computed once, fit fresh over
    exactly this call's `items` -- never `hashed_token_vector`'s single
    deterministic hash-bucket scheme (still used, unchanged, by every
    caller that doesn't reach this function).
    """
    if not items:
        return ()
    by_observation_id = {item.observation_id: item for item in items}
    vectors = case_tfidf_vectors(items)
    for item in items:
        await store.upsert(
            item,
            now=now,
            embedding=vectors[item.descriptor_id],
            provider=TFIDF_VECTOR_PROVIDER,
            provider_version=TFIDF_VECTOR_PROVIDER_VERSION,
            snapshot_hash=tfidf_snapshot_hash(item),
        )
    pairs: dict[tuple[str, str], tuple[ObservationDescriptor, ObservationDescriptor, float]] = {}
    for item in items:
        neighbors = await store.search(item, limit=limit, embedding=vectors[item.descriptor_id])
        for neighbor_observation_id, score in neighbors:
            if not (threshold <= score < 1.0):
                continue
            neighbor = by_observation_id.get(neighbor_observation_id)
            if neighbor is None or neighbor.observation_id == item.observation_id:
                continue
            if _same_origin_event(item, neighbor):
                continue
            left, right = _ordered(item, neighbor)
            key = (str(left.observation_id), str(right.observation_id))
            existing = pairs.get(key)
            if existing is None or score > existing[2]:
                pairs[key] = (left, right, score)
    return tuple(
        RetrievedCandidate(
            case_id=left.case_id,
            left_observation_id=left.observation_id,
            right_observation_id=right.observation_id,
            reasons=(RetrievalReason.VECTOR,),
            identifier_types=(),
            vector_score=score,
            supporting_observation_ids=(left.observation_id, right.observation_id),
            supporting_evidence_ids=(left.evidence_id, right.evidence_id),
            contradiction_reasons=(),
        )
        for left, right, score in sorted(
            pairs.values(),
            key=lambda triple: (str(triple[0].observation_id), str(triple[1].observation_id)),
        )
    )
