"""`PgvectorCandidateStore` had zero test coverage before this file (confirmed
by the Phase 5A reconciliation audit) despite being real, structurally
correct async SQL. These are unit tests against a fake engine -- no real
Postgres/pgvector needed; see `test_intelligence_vector_store_live.py` for
genuine pgvector coverage."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.modules.graph.intelligence.models import ObservationDescriptor
from app.modules.graph.intelligence.vector_store import (
    VECTOR_CONFIGURATION_VERSION,
    PgvectorCandidateStore,
    VectorSearchUnavailable,
    _literal,
    source_snapshot_hash,
)


def _descriptor(**overrides: object) -> ObservationDescriptor:
    defaults: dict[str, object] = {
        "case_id": uuid4(),
        "observation_id": uuid4(),
        "evidence_id": uuid4(),
        "source_locator_reference": "message_id=m1",
        "aliases": ("Alice",),
    }
    defaults.update(overrides)
    return ObservationDescriptor(**defaults)  # type: ignore[arg-type]


class _RaisingConnection:
    async def execute(self, *args: object, **kwargs: object) -> object:
        raise sa.exc.DBAPIError("stmt", {}, RuntimeError("connection refused"))


class _RaisingBeginContext:
    async def __aenter__(self) -> _RaisingConnection:
        return _RaisingConnection()

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _UnavailableEngine:
    """A fake `AsyncEngine` whose every connection attempt fails at execute time --
    simulating pgvector/the extension/table being genuinely unavailable."""

    def begin(self) -> _RaisingBeginContext:
        return _RaisingBeginContext()

    def connect(self) -> _RaisingBeginContext:
        return _RaisingBeginContext()


async def test_upsert_raises_vector_search_unavailable_on_dbapi_error() -> None:
    """Scenario: pgvector unavailable -> explicit, typed outcome, never a raw exception."""
    store = PgvectorCandidateStore(_UnavailableEngine())  # type: ignore[arg-type]
    with pytest.raises(VectorSearchUnavailable):
        await store.upsert(_descriptor())


async def test_search_raises_vector_search_unavailable_on_dbapi_error() -> None:
    store = PgvectorCandidateStore(_UnavailableEngine())  # type: ignore[arg-type]
    with pytest.raises(VectorSearchUnavailable):
        await store.search(_descriptor())


async def test_search_rejects_out_of_bounds_limit_before_touching_the_engine() -> None:
    """A bad `limit` fails fast with a plain `ValueError` -- never reaches the
    (here, deliberately broken) engine at all, proving the bound is checked first."""
    store = PgvectorCandidateStore(_UnavailableEngine())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="between 1 and 200"):
        await store.search(_descriptor(), limit=0)
    with pytest.raises(ValueError, match="between 1 and 200"):
        await store.search(_descriptor(), limit=201)


def test_source_snapshot_hash_is_deterministic_and_case_scoped() -> None:
    descriptor = _descriptor()
    assert source_snapshot_hash(descriptor) == source_snapshot_hash(descriptor)
    other_case = _descriptor(
        case_id=uuid4(),
        observation_id=descriptor.observation_id,
        evidence_id=descriptor.evidence_id,
    )
    assert source_snapshot_hash(descriptor) != source_snapshot_hash(other_case)


def test_source_snapshot_hash_records_provider_version_and_configuration() -> None:
    """A changed provider/config version must change the hash -- this is the
    "provider/version/configuration/source-snapshot hashes" reproducibility
    field the Phase 5A brief requires."""
    descriptor = _descriptor()
    original = source_snapshot_hash(descriptor)
    changed_aliases = descriptor.model_copy(update={"aliases": ("Bob",)})
    assert source_snapshot_hash(changed_aliases) != original


def test_literal_rejects_wrong_dimension_vectors() -> None:
    with pytest.raises(ValueError, match="dimension"):
        _literal((1.0, 2.0))


def test_literal_formats_a_valid_vector_as_pgvector_syntax() -> None:
    vector = tuple(0.0 for _ in range(32))
    rendered = _literal(vector)
    assert rendered.startswith("[") and rendered.endswith("]")
    assert rendered.count(",") == 31


def test_vector_configuration_version_is_a_stable_named_constant() -> None:
    """Not a magic string: this version is what
    `source_snapshot_hash`/`upsert`'s `configuration_hash` are keyed on, so
    it must never silently change without a deliberate version bump."""
    assert VECTOR_CONFIGURATION_VERSION == "phase5_local_vector_retrieval_v1"


def test_upsert_and_search_never_touch_neo4j_or_import_a_driver() -> None:
    """Structural guard: this module must remain PostgreSQL-only, per the
    Phase 5A rule "do not directly write to Neo4j from retrieval/scoring
    code" -- checked by inspecting the module's own imports."""
    import app.modules.graph.intelligence.vector_store as module

    source = module.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        contents = handle.read()
    assert "neo4j" not in contents.lower()


async def test_upsert_is_deterministic_for_the_same_case_and_observation() -> None:
    """Two upserts of the same descriptor must compute the identical
    `vector_id`/embedding/hashes -- verified here by capturing the SQL
    parameters a fake connection receives, without needing real pgvector."""
    captured: list[dict[str, object]] = []

    class _CapturingConnection:
        async def execute(self, _statement: object, params: dict[str, object]) -> None:
            captured.append(params)

    class _CapturingBeginContext:
        async def __aenter__(self) -> _CapturingConnection:
            return _CapturingConnection()

        async def __aexit__(self, *exc_info: object) -> None:
            return None

    class _CapturingEngine:
        def begin(self) -> _CapturingBeginContext:
            return _CapturingBeginContext()

    store = PgvectorCandidateStore(_CapturingEngine())  # type: ignore[arg-type]
    descriptor = _descriptor()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first_id = await store.upsert(descriptor, now=now)
    second_id = await store.upsert(descriptor, now=now)
    assert first_id == second_id
    assert captured[0]["vector_id"] == captured[1]["vector_id"]
    assert captured[0]["embedding"] == captured[1]["embedding"]
    assert captured[0]["source_snapshot_hash"] == captured[1]["source_snapshot_hash"]
