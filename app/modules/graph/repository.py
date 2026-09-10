"""Typed async Neo4j transaction boundary.

This is the only module in `app/modules/graph/` that touches the `neo4j`
driver directly. `projection.py`, `queries.py`, and `schema.py` build
parameterized Cypher + a parameter map and hand both to
`Neo4jGraphRepository`; they never see a driver session or a raw exception
from it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog
from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncManagedTransaction
from neo4j.exceptions import Neo4jError

from app.core.config import Settings
from app.modules.graph.errors import GraphConnectionError

logger = structlog.get_logger(__name__)

# A single Neo4j record decoded to a plain dict. This is the one place
# `Any` is unavoidable: driver records carry heterogeneous, dynamically-typed
# property values (str, int, float, bool, list, neo4j.time.DateTime, ...).
# Everything above this module converts these into typed Pydantic models
# (see `app/modules/graph/models.py`) before returning to a caller.
GraphRecord = dict[str, Any]


def create_driver(settings: Settings) -> AsyncDriver:
    """Build a Neo4j driver from application configuration.

    Credentials come from `Settings` only -- no other part of this module
    receives or stores them directly.
    """
    return AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_username, settings.neo4j_password),
    )


async def _run_and_collect(
    tx: AsyncManagedTransaction, query: str, parameters: Mapping[str, Any]
) -> list[GraphRecord]:
    result = await tx.run(query, dict(parameters))
    return [record.data() async for record in result]


class Neo4jGraphRepository:
    """Thin, typed transaction wrapper over an injected `AsyncDriver`.

    Every method takes fully-built, already-parameterized Cypher plus its
    parameter map -- this class performs no query construction and knows
    nothing about `app/contracts/`. Injecting the driver (rather than
    constructing one internally) is what lets tests substitute a fake/mock
    driver without a real Neo4j instance.
    """

    def __init__(self, driver: AsyncDriver) -> None:
        self._driver = driver

    async def close(self) -> None:
        """Close the underlying driver. Only the owner of the driver should call this."""
        await self._driver.close()

    async def write(self, query: str, parameters: Mapping[str, Any]) -> list[GraphRecord]:
        """Run `query` inside a single write transaction and return its records."""
        try:
            async with self._driver.session() as session:
                return await session.execute_write(_run_and_collect, query, parameters)
        except Neo4jError as exc:
            logger.warning("graph_write_failed", exc_type=type(exc).__name__)
            raise GraphConnectionError("graph write failed") from exc

    async def read(self, query: str, parameters: Mapping[str, Any]) -> list[GraphRecord]:
        """Run `query` inside a single read transaction and return its records."""
        try:
            async with self._driver.session() as session:
                return await session.execute_read(_run_and_collect, query, parameters)
        except Neo4jError as exc:
            logger.warning("graph_read_failed", exc_type=type(exc).__name__)
            raise GraphConnectionError("graph read failed") from exc
