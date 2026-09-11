"""Shared fixtures for `tests/integration/evidence_lifecycle/`.

Mirrors `tests/integration/access_control/conftest.py`'s pattern exactly:
skip the whole package if `.env` is absent, and skip (never fabricate a
pass) if PostgreSQL specifically isn't reachable through it. Applies the
Alembic migration once per test session (idempotent) before any test runs.
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
from app.dependencies.services import check_minio, check_postgres
from app.modules.access_control.models import CaseRecord, CaseStatus, ClearanceLevel
from app.modules.access_control.repository import (
    AccessControlRepository,
    cases_table,
)
from app.modules.access_control.repository import (
    create_engine as create_ac_engine,
)
from app.modules.evidence_lifecycle.repository import (
    EvidenceLifecycleRepository,
    create_engine,
    evidence_records_table,
    worker_jobs_table,
)
from app.modules.evidence_lifecycle.storage import MinioObjectStorage

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
async def repository() -> AsyncIterator[EvidenceLifecycleRepository]:
    settings = _live_settings()
    engine = create_engine(settings)
    try:
        yield EvidenceLifecycleRepository(engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def ac_repository() -> AsyncIterator[AccessControlRepository]:
    settings = _live_settings()
    engine = create_ac_engine(settings)
    try:
        yield AccessControlRepository(engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def minio_storage() -> MinioObjectStorage:
    """Skips (not fails) if MinIO specifically isn't reachable -- mirrors `redis_client`."""
    settings = _live_settings()
    try:
        await check_minio(settings)
    except Exception as exc:
        pytest.skip(f"minio not reachable: {type(exc).__name__}")
    return MinioObjectStorage(settings)


@pytest_asyncio.fixture
async def seeded_case(ac_repository: AccessControlRepository) -> AsyncIterator[UUID]:
    """A minimal real `cases` row so `evidence_records`/`worker_jobs` FKs are satisfiable."""
    from datetime import UTC, datetime

    case = CaseRecord(
        case_id=uuid4(),
        case_reference=f"CASE-EL-{uuid4().hex[:8]}",
        classification=ClearanceLevel.CONFIDENTIAL,
        status=CaseStatus.OPEN,
        created_at=datetime.now(UTC),
    )
    await ac_repository.create_case(case)
    yield case.case_id
    async with ac_repository._engine.begin() as conn:  # noqa: SLF001 - test-only cleanup
        await conn.execute(sa.delete(cases_table).where(cases_table.c.case_id == case.case_id))


@pytest_asyncio.fixture
async def seeded_user(ac_repository: AccessControlRepository) -> AsyncIterator[UUID]:
    """A minimal real `users` row so `evidence_records.uploaded_by`'s FK is satisfiable."""
    from datetime import UTC, datetime

    from app.modules.access_control.models import UserRecord
    from app.modules.access_control.password import hash_password
    from app.modules.access_control.repository import users_table

    now = datetime.now(UTC)
    user = UserRecord(
        user_id=uuid4(),
        email_normalized=f"evidence-lifecycle-live-{uuid4().hex[:8]}@example.test",
        display_name="Live Integration Test User",
        password_hash=hash_password("correct-horse-battery-staple"),
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    await ac_repository.create_user(user)
    yield user.user_id
    async with ac_repository._engine.begin() as conn:  # noqa: SLF001 - test-only cleanup
        await conn.execute(sa.delete(users_table).where(users_table.c.user_id == user.user_id))


@pytest_asyncio.fixture
async def cleanup_evidence_ids(
    repository: EvidenceLifecycleRepository,
) -> AsyncIterator[list[UUID]]:
    ids: list[UUID] = []
    yield ids
    if not ids:
        return
    async with repository._engine.begin() as conn:  # noqa: SLF001 - test-only cleanup
        await conn.execute(
            sa.delete(worker_jobs_table).where(worker_jobs_table.c.evidence_id.in_(ids))
        )
        await conn.execute(
            sa.delete(evidence_records_table).where(evidence_records_table.c.evidence_id.in_(ids))
        )
