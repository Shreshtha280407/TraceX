"""Shared fixtures for `tests/integration/graph/`.

Mirrors `tests/integration/test_readiness_live.py`'s pattern exactly: skip
the whole package if `.env` is absent, and skip (never fabricate a pass) if
Neo4j isn't actually reachable through it. Run
`docker compose up -d postgres neo4j redis minio` with a real `.env`
(copied from `.env.example`) to exercise this suite for real.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import dotenv_values

from app.core.config import Settings
from app.dependencies.services import check_neo4j, check_postgres
from app.modules.graph.outbox_repository import GraphProjectionOutboxRepository
from app.modules.graph.outbox_repository import create_engine as create_postgres_engine
from app.modules.graph.repository import Neo4jGraphRepository, create_driver

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env"

pytestmark = pytest.mark.skipif(
    not ENV_FILE.exists(),
    reason="no .env at repo root; copy .env.example and start infra to run this suite",
)


def _live_settings() -> Settings:
    values = dotenv_values(ENV_FILE)
    kwargs: dict[str, Any] = {k.lower(): v for k, v in values.items() if v is not None}
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def repository() -> AsyncIterator[Neo4jGraphRepository]:
    settings = _live_settings()
    try:
        await check_neo4j(settings)
    except Exception as exc:
        pytest.skip(f"neo4j not reachable: {type(exc).__name__}")

    driver = create_driver(settings)
    graph_repository = Neo4jGraphRepository(driver)
    try:
        yield graph_repository
    finally:
        await driver.close()


@pytest_asyncio.fixture
async def case_id(repository: Neo4jGraphRepository) -> AsyncIterator[UUID]:
    """A fresh case ID, cleaned up (and everything projected under it) after the test."""
    generated = uuid4()
    try:
        yield generated
    finally:
        await repository.write(
            "MATCH (n {case_id: $case_id}) DETACH DELETE n", {"case_id": str(generated)}
        )


# --- PostgreSQL-backed outbox fixtures (Phase 2 -- Shreshtha) ---------------
#
# Deliberately NOT autouse: only tests that actually need
# `GraphProjectionOutboxRepository` (test_outbox_repository_live.py) depend
# on `outbox_repository` below, so the existing Neo4j-only tests in this
# package are unaffected by, and don't skip because of, PostgreSQL
# specifically being unreachable.


def _clean_subprocess_env() -> dict[str, str]:
    """See `tests/integration/access_control/conftest.py::_clean_subprocess_env` for why."""
    from tests.conftest import _TEST_ENV_DEFAULTS

    env = dict(os.environ)
    for key in _TEST_ENV_DEFAULTS:
        env.pop(key, None)
    return env


async def _run_alembic_upgrade() -> None:
    repo_root = REPO_ROOT
    process = await asyncio.create_subprocess_exec(
        "uv",
        "run",
        "alembic",
        "upgrade",
        "head",
        cwd=str(repo_root),
        env=_clean_subprocess_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed:\n{stderr.decode()[-4000:]}")


@pytest_asyncio.fixture
async def outbox_repository() -> AsyncIterator[GraphProjectionOutboxRepository]:
    """A real `GraphProjectionOutboxRepository` against live PostgreSQL, migrated first.

    Skips (never fabricates a pass) if PostgreSQL specifically isn't
    reachable -- same pattern as `repository` above does for Neo4j.
    """
    settings = _live_settings()
    try:
        await check_postgres(settings)
    except Exception as exc:
        pytest.skip(f"postgres not reachable: {type(exc).__name__}")
    await _run_alembic_upgrade()

    engine = create_postgres_engine(settings)
    try:
        yield GraphProjectionOutboxRepository(engine)
    finally:
        await engine.dispose()
