"""The graph-projection worker entry point: a bounded-batch `--once` CLI.

    uv run python -m app.modules.graph.worker --once

Claims up to `GRAPH_PROJECTION_BATCH_SIZE` durable `graph_projection_jobs`
rows and attempts to project each one's canonical `ObservationV1` into
Neo4j, then exits -- no daemon, polling loop, or scheduler exists in this
phase (see "No continuous projector daemon" in
`docs/architecture/graph-projection.md`). Run it again to process another
batch; a cron/systemd timer or an operator's own script is expected to
invoke it repeatedly, exactly the same non-goal this repository's other
one-shot worker CLIs (`structured_processing.worker`,
`communication_processing.worker`) already document.

Unlike those extraction workers, this one talks to PostgreSQL and Neo4j
directly (`GraphProjectionOutboxRepository`, `Neo4jGraphRepository`) rather
than through the internal worker HTTP API -- it is a backend-owned internal
service, not an extractor worker bound by the "workers must not require
direct PostgreSQL/Neo4j/MinIO credentials" rule in `CLAUDE.md` (that rule
targets untrusted, arbitrary extraction code; this module is authored and
operated by the same team as the backend itself).

Exit code: `0` if every claimed job either succeeded or was left safely
retryable/deferred for a future run; `1` if any job reached a terminal
`failed` state (or nothing could be attempted due to a startup/connection
problem) -- so a cron/systemd wrapper can alert on it, without treating an
ordinary "Neo4j was briefly unavailable, will retry" run as an error.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

import structlog

from app.core.config import Settings, get_settings
from app.modules.graph.outbox_repository import GraphProjectionOutboxRepository
from app.modules.graph.outbox_repository import create_engine as create_postgres_engine
from app.modules.graph.projector import ProjectorRunSummary, run_batch
from app.modules.graph.repository import Neo4jGraphRepository, create_driver

logger = structlog.get_logger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def run_once(settings: Settings) -> ProjectorRunSummary:
    """Claim and attempt one bounded batch, then release both connections."""
    postgres_engine = create_postgres_engine(settings)
    outbox = GraphProjectionOutboxRepository(postgres_engine)
    neo4j_driver = create_driver(settings)
    graph = Neo4jGraphRepository(neo4j_driver)
    try:
        return await run_batch(
            outbox,
            graph,
            now=datetime.now(UTC),
            lease_seconds=settings.graph_projection_lease_seconds,
            batch_size=settings.graph_projection_batch_size,
        )
    finally:
        await outbox.close()
        await graph.close()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: `uv run python -m app.modules.graph.worker --once`."""
    parser = argparse.ArgumentParser(
        prog="python -m app.modules.graph.worker",
        description=(
            "Claim and attempt one bounded batch of durable graph-projection jobs, then exit. "
            "No daemon or polling mode exists in this phase."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        required=True,
        help="Run exactly one claim-and-project batch, then exit.",
    )
    parser.parse_args(argv)

    _configure_logging()
    settings = get_settings()

    try:
        summary = asyncio.run(run_once(settings))
    except Exception as exc:  # a startup/connection failure, not a per-job outcome
        logger.error("graph.worker.run_failed", exc_type=type(exc).__name__)
        return 1

    logger.info(
        "graph.worker.run_completed",
        claimed=summary.claimed,
        succeeded=summary.succeeded,
        failed=summary.failed,
        retrying=summary.retrying,
        deferred=summary.deferred,
    )
    return 1 if summary.failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
