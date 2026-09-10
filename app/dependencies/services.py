"""Readiness check adapters for infrastructure dependencies.

Each adapter is a small async function that raises on failure and returns
nothing on success. They take no FastAPI-specific arguments beyond
`Settings`, so they can be called and mocked directly in tests without any
web-layer plumbing. `get_health_checks` is the FastAPI dependency that
binds them to the active settings; tests override it via
`app.dependency_overrides[get_health_checks]` to simulate healthy/unhealthy
dependencies without real infrastructure.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated

import redis.asyncio as redis
from fastapi import Depends
from minio import Minio
from neo4j import AsyncGraphDatabase
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings, get_settings

type HealthCheckMap = dict[str, Callable[[], Awaitable[None]]]

_CONNECT_TIMEOUT_SECONDS = 3.0


async def check_postgres(settings: Settings) -> None:
    """Raise if a minimal PostgreSQL connection cannot be established.

    Uses SQLAlchemy's async engine (not raw `asyncpg.connect`) because
    `postgres_dsn` uses the SQLAlchemy-style `postgresql+asyncpg://` scheme
    everywhere else in this codebase (config, Alembic) — `asyncpg.connect`
    doesn't understand the `+asyncpg` driver suffix and rejects that DSN.
    """
    engine = create_async_engine(
        str(settings.postgres_dsn),
        connect_args={"timeout": _CONNECT_TIMEOUT_SECONDS},
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


async def check_neo4j(settings: Settings) -> None:
    """Raise if Neo4j cannot be reached and does not answer a trivial query."""
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_username, settings.neo4j_password),
        connection_timeout=_CONNECT_TIMEOUT_SECONDS,
    )
    try:
        async with driver.session() as session:
            await session.run("RETURN 1")
    finally:
        await driver.close()


async def check_redis(settings: Settings) -> None:
    """Raise if Redis does not respond to PING."""
    client = redis.from_url(str(settings.redis_url), socket_timeout=_CONNECT_TIMEOUT_SECONDS)
    try:
        await client.ping()
    finally:
        await client.aclose()


async def check_minio(settings: Settings) -> None:
    """Raise if the configured MinIO endpoint cannot be reached.

    `minio-py` is a synchronous client; the call is offloaded to a thread
    so it does not block the event loop.
    """
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )
    await asyncio.to_thread(client.bucket_exists, settings.minio_bucket)


def get_health_checks(
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthCheckMap:
    """Bind each infrastructure check to the active settings."""
    return {
        "postgres": lambda: check_postgres(settings),
        "neo4j": lambda: check_neo4j(settings),
        "redis": lambda: check_redis(settings),
        "minio": lambda: check_minio(settings),
    }
