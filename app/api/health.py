"""Health, readiness, and contract-version metadata endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import PlainTextResponse

from app.contracts import CONTRACT_VERSIONS
from app.core.config import Settings, get_settings
from app.core.errors import ErrorCode, error_body, get_request_id
from app.dependencies.services import HealthCheckMap, get_health_checks
from app.modules.access_control.dependencies import get_access_control_repository
from app.modules.access_control.models import WorkerLivenessStatus, worker_liveness_status
from app.modules.access_control.repository import AccessControlRepository

router = APIRouter()
logger = structlog.get_logger(__name__)

SERVICE_NAME = "tracex-api"
SERVICE_VERSION = "0.1.0"


async def _worker_process_liveness(
    repository: AccessControlRepository, settings: Settings, *, postgres_ok: bool
) -> dict[str, object]:
    """Gap-Closure WP-6 (G17): real observed liveness, from the heartbeat
    registry `require_worker_principal` populates on every successful
    worker authentication (see `evidence_lifecycle/dependencies.py`).

    Still never a claim that a specific worker *process* is alive right
    now -- only "was this credential used successfully within the
    configured staleness window." Reports `"not_observed"` only when it
    truly cannot ask (Postgres itself is unreachable), never as a
    permanent, hardcoded limitation.
    """
    if not postgres_ok:
        return {"status": "not_observed", "reason": "postgres_unavailable"}
    try:
        credentials = await repository.list_worker_credentials()
    except Exception as exc:
        logger.warning("readiness_worker_liveness_query_failed", exc_type=type(exc).__name__)
        return {"status": "not_observed", "reason": "query_failed"}
    now = datetime.now(UTC)
    counts = {status_.value: 0 for status_ in WorkerLivenessStatus}
    for credential in credentials:
        liveness = worker_liveness_status(
            credential, now=now, stale_seconds=settings.worker_heartbeat_stale_seconds
        )
        counts[liveness.value] += 1
    return {"status": "observed", "total_workers": len(credentials), "by_liveness": counts}


async def _operational_components(
    results: dict[str, bool], repository: AccessControlRepository, settings: Settings
) -> dict[str, dict[str, object]]:
    """Safe derived capability health."""

    def status_for(*dependencies: str) -> str:
        return "ok" if all(results.get(name, False) for name in dependencies) else "unavailable"

    worker_liveness = await _worker_process_liveness(
        repository, settings, postgres_ok=results.get("postgres", False)
    )
    return {
        "worker_control_plane": {
            "status": status_for("postgres", "redis", "minio"),
            "dependencies": ["postgres", "redis", "minio"],
            "worker_process_liveness": worker_liveness,
        },
        "graph_projection": {
            "status": status_for("postgres", "neo4j"),
            "dependencies": ["postgres", "neo4j"],
        },
        "correlation_outbox": {
            "status": status_for("postgres", "neo4j"),
            "dependencies": ["postgres", "neo4j"],
        },
    }


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
    repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    """Readiness: required infrastructure dependencies are reachable.

    Returns 200 only when every check succeeds; otherwise 503 with a safe,
    structured error body. Never includes connection strings, credentials,
    or exception detail — only per-dependency ok/unavailable status.
    """
    results = dict(await asyncio.gather(*(_probe(name, fn) for name, fn in checks.items())))
    dependencies = {name: ("ok" if ok else "unavailable") for name, ok in results.items()}
    components = await _operational_components(results, repository, settings)
    healthy = all(results.values())

    if healthy:
        response.status_code = status.HTTP_200_OK
        return {"status": "ok", "dependencies": dependencies, "components": components}

    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    body = error_body(
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        "One or more required services are unavailable",
        get_request_id(),
    )
    body["dependencies"] = dependencies
    body["components"] = components
    return body


@router.get("/api/v1/meta/contracts")
async def get_contract_versions() -> dict[str, str]:
    """Report the shared contract versions this API build supports."""
    return CONTRACT_VERSIONS


#: A metric line's `# HELP`/`# TYPE` comment pair plus its value line(s),
#: in the plain-text Prometheus exposition format
#: (https://prometheus.io/docs/instrumenting/exposition_formats/).
#: Hand-rolled rather than adding a `prometheus_client` dependency -- the
#: format is a handful of fixed-shape lines for a small, bounded metric
#: set; a new third-party dependency was judged unjustified for that.
def _render_prometheus_text(
    *,
    name: str,
    help_text: str,
    metric_type: str,
    samples: list[tuple[dict[str, str], float]],
) -> str:
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}"]
    for labels, value in samples:
        label_text = ",".join(f'{k}="{v}"' for k, v in labels.items())
        suffix = f"{{{label_text}}}" if label_text else ""
        lines.append(f"{name}{suffix} {value}")
    return "\n".join(lines)


@router.get("/metrics")
async def metrics(
    checks: Annotated[HealthCheckMap, Depends(get_health_checks)],
    repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> PlainTextResponse:
    """Gap-Closure WP-6/re-close (G16): Prometheus text-format operational
    metrics. Deliberately bounded to process/dependency/worker-fleet-wide
    aggregates only -- never a per-case count, a case_id label, or any
    other case-scoped value. A case-scoped metric here would violate this
    codebase's own "every read/query is scoped to one case_id" rule by
    definition (a global metrics endpoint has no case_id to scope to);
    the discipline instead is simply never emitting one.
    """
    results = dict(await asyncio.gather(*(_probe(name, fn) for name, fn in checks.items())))
    try:
        credentials = await repository.list_worker_credentials()
    except Exception as exc:  # noqa: BLE001 - metrics must never 500 on a query failure
        logger.warning("metrics_worker_query_failed", exc_type=type(exc).__name__)
        credentials = []
    now = datetime.now(UTC)
    liveness_counts = {status_.value: 0 for status_ in WorkerLivenessStatus}
    credential_status_counts: dict[str, int] = {}
    for credential in credentials:
        liveness = worker_liveness_status(
            credential, now=now, stale_seconds=settings.worker_heartbeat_stale_seconds
        )
        liveness_counts[liveness.value] += 1
        credential_status_counts[credential.status.value] = (
            credential_status_counts.get(credential.status.value, 0) + 1
        )

    body = "\n\n".join(
        [
            _render_prometheus_text(
                name="tracex_up",
                help_text="TraceX API process liveness (always 1 if this response was sent).",
                metric_type="gauge",
                samples=[({}, 1)],
            ),
            _render_prometheus_text(
                name="tracex_dependency_up",
                help_text=(
                    "Whether a required infrastructure dependency was reachable "
                    "at last check (1=ok, 0=unavailable)."
                ),
                metric_type="gauge",
                samples=[
                    ({"dependency": name}, 1 if ok else 0) for name, ok in sorted(results.items())
                ],
            ),
            _render_prometheus_text(
                name="tracex_worker_credentials_total",
                help_text="Provisioned worker credentials, by credential status.",
                metric_type="gauge",
                samples=[
                    ({"status": status_name}, count)
                    for status_name, count in sorted(credential_status_counts.items())
                ],
            ),
            _render_prometheus_text(
                name="tracex_worker_liveness_total",
                help_text=(
                    "Provisioned worker credentials, by observed heartbeat "
                    "liveness (active/stale/never_seen)."
                ),
                metric_type="gauge",
                samples=[
                    ({"liveness": liveness_name}, count)
                    for liveness_name, count in sorted(liveness_counts.items())
                ],
            ),
        ]
    )
    return PlainTextResponse(body + "\n", media_type="text/plain; version=0.0.4; charset=utf-8")
