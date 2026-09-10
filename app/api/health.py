"""Health, readiness, and contract-version metadata endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Response, status

from app.contracts import CONTRACT_VERSIONS
from app.core.errors import ErrorCode, error_body, get_request_id
from app.dependencies.services import HealthCheckMap, get_health_checks

router = APIRouter()
logger = structlog.get_logger(__name__)

SERVICE_NAME = "tracex-api"
SERVICE_VERSION = "0.1.0"


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the FastAPI process is up. Never checks dependencies."""
    return {"status": "ok", "service": SERVICE_NAME, "version": SERVICE_VERSION}


async def _probe(name: str, check: Callable[[], Awaitable[None]]) -> tuple[str, bool]:
    try:
        await check()
    except Exception as exc:
        # Log the exception *type* only, never its message: some drivers
        # embed the connection string/credentials in their error text.
        logger.warning("readiness_check_failed", dependency=name, exc_type=type(exc).__name__)
        return name, False
    return name, True


@router.get("/readyz")
async def readyz(
    response: Response,
    checks: Annotated[HealthCheckMap, Depends(get_health_checks)],
) -> dict[str, object]:
    """Readiness: required infrastructure dependencies are reachable.

    Returns 200 only when every check succeeds; otherwise 503 with a safe,
    structured error body. Never includes connection strings, credentials,
    or exception detail — only per-dependency ok/unavailable status.
    """
    results = dict(await asyncio.gather(*(_probe(name, fn) for name, fn in checks.items())))
    dependencies = {name: ("ok" if ok else "unavailable") for name, ok in results.items()}
    healthy = all(results.values())

    if healthy:
        response.status_code = status.HTTP_200_OK
        return {"status": "ok", "dependencies": dependencies}

    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    body = error_body(
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        "One or more required services are unavailable",
        get_request_id(),
    )
    body["dependencies"] = dependencies
    return body


@router.get("/api/v1/meta/contracts")
async def get_contract_versions() -> dict[str, str]:
    """Report the shared contract versions this API build supports."""
    return CONTRACT_VERSIONS
