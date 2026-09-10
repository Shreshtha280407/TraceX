"""GRAPH-SCHEMA-001 (live): applying the graph schema twice is safe.

Self-skips if `.env`/Neo4j aren't available -- see `conftest.py`.
"""

from __future__ import annotations

from app.modules.graph.repository import Neo4jGraphRepository
from app.modules.graph.schema import apply_schema, verify_schema


async def test_apply_schema_twice_succeeds_and_verifies(
    repository: Neo4jGraphRepository,
) -> None:
    first = await apply_schema(repository)
    second = await apply_schema(repository)
    assert first.applied == second.applied
    assert len(first.applied) > 0

    verify_result = await verify_schema(repository)
    assert verify_result.ok, f"missing schema objects: {verify_result.missing}"
