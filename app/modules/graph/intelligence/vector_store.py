"""Optional PostgreSQL pgvector persistence and bounded same-case retrieval."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.canonical import canonical_sha256
from app.core.ids import deterministic_uuid
from app.modules.graph.intelligence.models import ObservationDescriptor
from app.modules.graph.intelligence.retrieval import (
    VECTOR_DIMENSIONS,
    VECTOR_PROVIDER,
    VECTOR_PROVIDER_VERSION,
    hashed_token_vector,
)

VECTOR_CONFIGURATION_VERSION = "phase5_local_vector_retrieval_v1"


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


class PgvectorCandidateStore:
    """Stores deterministic local vectors; only PostgreSQL is contacted here."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def upsert(
        self, descriptor: ObservationDescriptor, *, now: datetime | None = None
    ) -> UUID:
        now = now or datetime.now(UTC)
        snapshot_hash = source_snapshot_hash(descriptor)
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
                        "provider": VECTOR_PROVIDER,
                        "provider_version": VECTOR_PROVIDER_VERSION,
                        "configuration_hash": canonical_sha256(
                            {
                                "version": VECTOR_CONFIGURATION_VERSION,
                                "dimensions": VECTOR_DIMENSIONS,
                            }
                        ),
                        "source_snapshot_hash": snapshot_hash,
                        "embedding": _literal(hashed_token_vector(descriptor)),
                        "created_at": now,
                    },
                )
        except sa.exc.DBAPIError as exc:
            raise VectorSearchUnavailable("pgvector candidate search is unavailable") from exc
        return vector_id

    async def search(
        self, descriptor: ObservationDescriptor, *, limit: int = 20
    ) -> list[tuple[UUID, float]]:
        if not 1 <= limit <= 200:
            raise ValueError("pgvector candidate limit must be between 1 and 200")
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
                            "embedding": _literal(hashed_token_vector(descriptor)),
                            "limit": limit,
                        },
                    )
                ).mappings()
        except sa.exc.DBAPIError as exc:
            raise VectorSearchUnavailable("pgvector candidate search is unavailable") from exc
        return [(UUID(str(row["observation_id"])), float(row["score"])) for row in rows]
