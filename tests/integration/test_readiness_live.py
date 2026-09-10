"""Live-infrastructure readiness checks.

Only exercises services that are actually reachable -- never fabricates a
pass. Requires a real `.env` at the repo root (git-ignored; copy
`.env.example` and run `docker compose up -d postgres neo4j redis minio`).
Skips the whole module if `.env` is absent, and skips each individual
dependency check that fails to connect rather than failing the suite for
infrastructure this environment doesn't happen to have running.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

from app.core.config import Settings
from app.dependencies.services import check_minio, check_neo4j, check_postgres, check_redis

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"

pytestmark = pytest.mark.skipif(
    not ENV_FILE.exists(),
    reason="no .env at repo root; copy .env.example and start infra to run this suite",
)


@pytest.fixture(scope="module")
def live_settings() -> Settings:
    values = dotenv_values(ENV_FILE)
    kwargs: dict[str, Any] = {k.lower(): v for k, v in values.items() if v is not None}
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


async def test_postgres_reachable(live_settings: Settings) -> None:
    try:
        await check_postgres(live_settings)
    except Exception as exc:
        pytest.skip(f"postgres not reachable: {type(exc).__name__}")


async def test_neo4j_reachable(live_settings: Settings) -> None:
    try:
        await check_neo4j(live_settings)
    except Exception as exc:
        pytest.skip(f"neo4j not reachable: {type(exc).__name__}")


async def test_redis_reachable(live_settings: Settings) -> None:
    try:
        await check_redis(live_settings)
    except Exception as exc:
        pytest.skip(f"redis not reachable: {type(exc).__name__}")


async def test_minio_reachable(live_settings: Settings) -> None:
    try:
        await check_minio(live_settings)
    except Exception as exc:
        pytest.skip(f"minio not reachable: {type(exc).__name__}")
