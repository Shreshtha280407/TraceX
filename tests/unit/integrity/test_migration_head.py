"""Proof point 18 (static half): exactly one Alembic head after this migration.

`alembic heads`/`history` only read the migration dependency graph on disk --
no database connection is required, so this runs in the default unit suite.
The live "upgrade actually succeeds against real PostgreSQL" half is
exercised by every `tests/integration/*/conftest.py`'s `_migrated_database`
fixture, including `tests/integration/integrity/conftest.py`'s.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_exactly_one_alembic_head() -> None:
    result = subprocess.run(
        ["uv", "run", "alembic", "heads"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    heads = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1, f"expected exactly one alembic head, got: {heads}"


def test_latest_gap_closure_migration_is_the_current_head() -> None:
    result = subprocess.run(
        ["uv", "run", "alembic", "heads"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    # Updated for ADR-033's additive users.must_change_password/totp_secret/
    # totp_enabled migration, which now sits on top of ADR-031's
    # hypotheses.supporting_entity_resolution_candidate_ids migration
    # (`7a8b9c0d1e2f`) -- see `alembic history` for the unbroken chain back
    # through Gap-Closure WP-6's `6f7a8b9c0d1e` and Phase 6 Part 5's
    # `a3b4c5d6e7f8`.
    assert "b2c3d4e5f6a7" in result.stdout
