"""Scenario 1: Alembic migration applies to live PostgreSQL and creates the expected tables.

The `_migrated_database` autouse fixture in `conftest.py` already applied
the migration before this test runs; this test verifies the result and
that re-applying is a safe no-op.
"""

from __future__ import annotations

import sqlalchemy as sa

from app.modules.access_control.repository import AccessControlRepository
from tests.integration.access_control.conftest import _run_alembic_upgrade

_EXPECTED_TABLES = {"users", "cases", "case_memberships", "auth_sessions", "security_audit_events"}


async def test_migration_creates_all_expected_tables(repository: AccessControlRepository) -> None:
    async with repository._engine.connect() as conn:  # noqa: SLF001 - test-only introspection
        rows = (
            await conn.execute(
                sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = ANY(:names)"
                ),
                {"names": list(_EXPECTED_TABLES)},
            )
        ).all()
    found = {row[0] for row in rows}
    assert found == _EXPECTED_TABLES


async def test_migration_is_idempotent_to_reapply() -> None:
    # alembic upgrade to an already-current head must be a safe no-op.
    await _run_alembic_upgrade()
