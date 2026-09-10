"""Shared fixtures for `tests/integration/access_control/`.

Mirrors `tests/integration/test_readiness_live.py` and
`tests/integration/graph/conftest.py`'s pattern exactly: skip the whole
package if `.env` is absent, and skip (never fabricate a pass) if
PostgreSQL specifically isn't reachable through it. Run
`docker compose up -d postgres redis` with a real `.env` (copied from
`.env.example`) to exercise this suite for real.

Applies the Alembic migration once per test session (scenario 1: "Apply
Alembic migration to live PostgreSQL") before any test runs -- `alembic
upgrade head` is idempotent, so this is safe even if a previous run
already applied it.
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
import sqlalchemy as sa
from dotenv import dotenv_values

from app.core.config import Settings
from app.dependencies.services import check_postgres, check_redis
from app.modules.access_control.repository import (
    AccessControlRepository,
    cases_table,
    create_engine,
    users_table,
)

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


def _clean_subprocess_env() -> dict[str, str]:
    """`os.environ` with `tests/conftest.py`'s fake defaults removed.

    `tests/conftest.py` sets `POSTGRES_DSN` et al. via `os.environ.setdefault(...)`
    for the whole pytest session, so `os.environ` in *this* process
    already carries fake test values. `migrations/env.py` builds its DB
    URL via the module-level `app.core.config.get_settings()` (no way for
    a test to inject a custom `Settings` into it), and pydantic-settings
    resolves an already-set environment variable *before* falling back to
    `.env` -- so running `alembic` in-process here would silently target
    the fake `tracex_test` DSN instead of this suite's real `.env`.
    Running it as a genuinely separate subprocess with those specific keys
    stripped (importing the exact list from `tests.conftest` so the two
    can never drift) lets its own `Settings()` fall through to `.env`.
    """
    from tests.conftest import _TEST_ENV_DEFAULTS

    env = dict(os.environ)
    for key in _TEST_ENV_DEFAULTS:
        env.pop(key, None)
    return env


async def _run_alembic_upgrade() -> None:
    process = await asyncio.create_subprocess_exec(
        "uv",
        "run",
        "alembic",
        "upgrade",
        "head",
        cwd=str(REPO_ROOT),
        env=_clean_subprocess_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed:\n{stderr.decode()[-4000:]}")


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _migrated_database() -> None:
    settings = _live_settings()
    try:
        await check_postgres(settings)
    except Exception as exc:
        pytest.skip(f"postgres not reachable: {type(exc).__name__}")
    await _run_alembic_upgrade()


@pytest_asyncio.fixture
async def repository() -> AsyncIterator[AccessControlRepository]:
    settings = _live_settings()
    engine = create_engine(settings)
    try:
        yield AccessControlRepository(engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[Any]:
    """Skips (not fails) if Redis specifically isn't reachable -- see scenario 6."""
    import redis.asyncio as redis

    settings = _live_settings()
    try:
        await check_redis(settings)
    except Exception as exc:
        pytest.skip(f"redis not reachable: {type(exc).__name__}")
    client = redis.from_url(str(settings.redis_url))
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def cleanup_user_ids(repository: AccessControlRepository) -> AsyncIterator[list[UUID]]:
    """Collect user IDs created during a test; deletes them (cascades to memberships/sessions)."""
    ids: list[UUID] = []
    yield ids
    if not ids:
        return
    async with repository._engine.begin() as conn:  # noqa: SLF001 - test-only cleanup
        # `sa.delete(...).where(col.in_(ids))` binds through the column's
        # own `postgresql.UUID(as_uuid=True)` type -- a raw `sa.text(...
        # ANY(:ids))` with a bare Python list of `uuid.UUID` objects has no
        # such type information to bind against and silently matches zero
        # rows instead of raising, which is exactly what happened here
        # before this fix (confirmed via a leftover-row count after a full
        # local test run -- see `docs/qa/test-results.md`).
        await conn.execute(sa.delete(users_table).where(users_table.c.user_id.in_(ids)))


@pytest_asyncio.fixture
async def cleanup_case_ids(repository: AccessControlRepository) -> AsyncIterator[list[UUID]]:
    """Collect case IDs created during a test; deletes them (cascades to memberships)."""
    ids: list[UUID] = []
    yield ids
    if not ids:
        return
    async with repository._engine.begin() as conn:  # noqa: SLF001 - test-only cleanup
        await conn.execute(sa.delete(cases_table).where(cases_table.c.case_id.in_(ids)))


def unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}@example.test"
