"""Shared fixtures for `tests/integration/integrity/`.

Mirrors `tests/integration/evidence_lifecycle/conftest.py`'s pattern: skip
the whole package if `.env` is absent, and skip (never fabricate a pass) if
PostgreSQL specifically isn't reachable through it. Applies the Alembic
migration once per test session (idempotent) before any test runs.

Unlike evidence lifecycle, `integrity_events`/`merkle_checkpoints` carry no
foreign key to a `cases` row -- case scoping here is a bare UUID, matching
`correlation_records.case_id`'s existing precedent -- so no seeded
case/user fixture is needed.
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
from app.dependencies.services import check_postgres
from app.modules.integrity.repository import IntegrityRepository, create_engine
from app.modules.integrity.service import IntegrityService
from app.modules.integrity.signing import generate_signing_key_b64

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env"

pytestmark = pytest.mark.skipif(
    not ENV_FILE.exists(),
    reason="no .env at repo root; copy .env.example and start infra to run this suite",
)


def _live_settings(**overrides: Any) -> Settings:
    values = dotenv_values(ENV_FILE)
    kwargs: dict[str, Any] = {k.lower(): v for k, v in values.items() if v is not None}
    kwargs.update(overrides)
    # A real, freshly generated dev-only key -- never the operator's own
    # `.env` key, so these tests never depend on (or risk logging) it.
    # `.env.example` ships `INTEGRITY_SIGNING_KEY=` (present, empty) rather
    # than omitting the line, so a blank value must be treated the same as
    # an absent one here -- `setdefault` alone would never fire against a
    # fresh `cp .env.example .env` (see `Settings._normalize_blank_worker_
    # secret_to_none`'s docstring for the same empty-vs-absent distinction).
    if not str(kwargs.get("integrity_signing_key") or "").strip():
        kwargs["integrity_signing_key"] = generate_signing_key_b64()
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


def _clean_subprocess_env() -> dict[str, str]:
    """See `tests/integration/access_control/conftest.py::_clean_subprocess_env` for why."""
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
async def settings() -> Settings:
    return _live_settings()


@pytest_asyncio.fixture
async def repository(settings: Settings) -> AsyncIterator[IntegrityRepository]:
    engine = create_engine(settings)
    try:
        yield IntegrityRepository(engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def service(repository: IntegrityRepository, settings: Settings) -> IntegrityService:
    return IntegrityService(repository, settings)


@pytest_asyncio.fixture
async def case_id(repository: IntegrityRepository) -> AsyncIterator[UUID]:
    """A fresh, isolated case ID.

    Phase 6 Part 2 deliberately makes integrity records impossible to clean
    up with ordinary SQL; integration databases are disposable and each test
    gets an unguessable scope instead.
    """
    generated = uuid4()
    yield generated
