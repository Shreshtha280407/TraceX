"""Idempotent Neo4j schema management for the graph foundation.

Every statement uses `IF NOT EXISTS`, so `apply_schema` can be run any
number of times safely -- it is never run automatically during normal
FastAPI startup (see `app/main.py`, which does not import this module).
Run it explicitly:

    uv run python -m app.modules.graph.schema apply
    uv run python -m app.modules.graph.schema verify

See `docs/runbooks/local-development.md` for the full local workflow.

Constraint choice: Neo4j Community Edition (pinned in `compose.yaml` as
`neo4j:5.25-community`) supports composite *uniqueness* constraints
(`IS UNIQUE` over a property tuple) but not existence or node-key
constraints -- those remain Enterprise-only. Every non-`Case` label is
therefore given a composite uniqueness constraint over `(case_id,
<domain>_id)`, never a bare uniqueness constraint on the domain ID alone --
that compound identity is what makes case isolation a database-level
guarantee instead of an application-level convention (see
`docs/decisions/ADR-001-graph-projection-and-case-isolation.md`).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from typing import Literal

from app.core.config import get_settings
from app.modules.graph.repository import Neo4jGraphRepository, create_driver


@dataclass(frozen=True)
class SchemaStatement:
    """One idempotent DDL statement and the name Neo4j tracks it under."""

    name: str
    kind: Literal["constraint", "index"]
    cypher: str


CONSTRAINT_STATEMENTS: tuple[SchemaStatement, ...] = (
    SchemaStatement(
        name="case_case_id_unique",
        kind="constraint",
        cypher=(
            "CREATE CONSTRAINT case_case_id_unique IF NOT EXISTS "
            "FOR (c:Case) REQUIRE c.case_id IS UNIQUE"
        ),
    ),
    SchemaStatement(
        name="evidence_case_evidence_unique",
        kind="constraint",
        cypher=(
            "CREATE CONSTRAINT evidence_case_evidence_unique IF NOT EXISTS "
            "FOR (e:Evidence) REQUIRE (e.case_id, e.evidence_id) IS UNIQUE"
        ),
    ),
    SchemaStatement(
        name="observation_case_observation_unique",
        kind="constraint",
        cypher=(
            "CREATE CONSTRAINT observation_case_observation_unique IF NOT EXISTS "
            "FOR (o:Observation) REQUIRE (o.case_id, o.observation_id) IS UNIQUE"
        ),
    ),
    SchemaStatement(
        name="entity_case_entity_unique",
        kind="constraint",
        cypher=(
            "CREATE CONSTRAINT entity_case_entity_unique IF NOT EXISTS "
            "FOR (n:Entity) REQUIRE (n.case_id, n.entity_id) IS UNIQUE"
        ),
    ),
    SchemaStatement(
        name="event_case_event_unique",
        kind="constraint",
        cypher=(
            "CREATE CONSTRAINT event_case_event_unique IF NOT EXISTS "
            "FOR (v:Event) REQUIRE (v.case_id, v.event_id) IS UNIQUE"
        ),
    ),
)

INDEX_STATEMENTS: tuple[SchemaStatement, ...] = (
    SchemaStatement(
        name="observation_case_id_idx",
        kind="index",
        cypher=(
            "CREATE INDEX observation_case_id_idx IF NOT EXISTS FOR (o:Observation) ON (o.case_id)"
        ),
    ),
    SchemaStatement(
        name="observation_event_time_idx",
        kind="index",
        cypher=(
            "CREATE INDEX observation_event_time_idx IF NOT EXISTS "
            "FOR (o:Observation) ON (o.event_time)"
        ),
    ),
    SchemaStatement(
        name="event_case_id_idx",
        kind="index",
        cypher="CREATE INDEX event_case_id_idx IF NOT EXISTS FOR (v:Event) ON (v.case_id)",
    ),
    SchemaStatement(
        name="event_event_time_idx",
        kind="index",
        cypher="CREATE INDEX event_event_time_idx IF NOT EXISTS FOR (v:Event) ON (v.event_time)",
    ),
    SchemaStatement(
        name="entity_case_id_idx",
        kind="index",
        cypher="CREATE INDEX entity_case_id_idx IF NOT EXISTS FOR (n:Entity) ON (n.case_id)",
    ),
    SchemaStatement(
        name="entity_entity_type_idx",
        kind="index",
        cypher=(
            "CREATE INDEX entity_entity_type_idx IF NOT EXISTS FOR (n:Entity) ON (n.entity_type)"
        ),
    ),
)


def get_schema_statements() -> tuple[SchemaStatement, ...]:
    """All schema statements this module owns, constraints before indexes."""
    return CONSTRAINT_STATEMENTS + INDEX_STATEMENTS


@dataclass(frozen=True)
class SchemaApplyResult:
    applied: tuple[str, ...]


@dataclass(frozen=True)
class SchemaVerifyResult:
    present: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.missing


async def apply_schema(repository: Neo4jGraphRepository) -> SchemaApplyResult:
    """Run every constraint/index statement. Safe to call repeatedly.

    Each statement runs as its own transaction (not batched together):
    Neo4j does not allow multiple schema-altering operations inside one
    explicit transaction, so statements are applied one at a time,
    constraints before indexes.
    """
    for statement in get_schema_statements():
        await repository.write(statement.cypher, {})
    return SchemaApplyResult(applied=tuple(s.name for s in get_schema_statements()))


async def verify_schema(repository: Neo4jGraphRepository) -> SchemaVerifyResult:
    """Check that every expected constraint/index name is present in Neo4j."""
    constraint_rows = await repository.read("SHOW CONSTRAINTS YIELD name", {})
    index_rows = await repository.read("SHOW INDEXES YIELD name", {})
    existing_names = {row["name"] for row in constraint_rows} | {row["name"] for row in index_rows}

    expected_names = {s.name for s in get_schema_statements()}
    present = tuple(sorted(expected_names & existing_names))
    missing = tuple(sorted(expected_names - existing_names))
    return SchemaVerifyResult(present=present, missing=missing)


async def _run_cli(action: str) -> int:
    settings = get_settings()
    driver = create_driver(settings)
    repository = Neo4jGraphRepository(driver)
    try:
        if action == "apply":
            result = await apply_schema(repository)
            print(f"Applied {len(result.applied)} schema statements:")
            for name in result.applied:
                print(f"  - {name}")
            return 0

        verify_result = await verify_schema(repository)
        print(f"Present ({len(verify_result.present)}):")
        for name in verify_result.present:
            print(f"  - {name}")
        if verify_result.missing:
            print(f"Missing ({len(verify_result.missing)}):")
            for name in verify_result.missing:
                print(f"  - {name}")
            return 1
        print("All expected schema statements are present.")
        return 0
    finally:
        await repository.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="TraceX graph schema management")
    parser.add_argument("action", choices=["apply", "verify"])
    args = parser.parse_args()
    exit_code = asyncio.run(_run_cli(args.action))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
