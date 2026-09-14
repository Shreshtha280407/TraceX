"""The Phase 5 intelligence worker: two independent, explicit CLI actions.

    uv run python -m app.modules.graph.intelligence_worker --replay-once
    uv run python -m app.modules.graph.intelligence_worker --replay-loop
    uv run python -m app.modules.graph.intelligence_worker --generate --case-id <uuid>

This is the minimal composition/registration wiring the Phase 5A
reconciliation audit found missing: `intelligence/projection.py`'s semantic
`correlation.upserted.v1` handler and `integration_projector.
replay_graph_updates` both already existed, fully implemented and unit
tested, but nothing in `app/` ever composed them together or called either
from a reachable process -- every real call site was a test. This file adds
exactly that composition, nothing else. It does not change Nipun's durable
outbox semantics, table meaning, or replay/idempotency contract in any way.

`--replay-once`/`--replay-loop` mirror `graph.worker`'s existing
`graph_projection_jobs` CLI shape (same claim/lease/batch pattern, same
exit-code convention) applied to the *separate*, additive
`graph_update_events` outbox instead -- the two outboxes remain
intentionally independent (see `docs/architecture/phase-5-integration.md`).

`--generate` runs `intelligence.pipeline.run_case_correlation_pass` for one
explicit case -- the retrieval -> scoring -> correlation -> submit pipeline
this same audit found unwired. There is no "claim any case" queue for this
action in Phase 5A: an operator or a future scheduler names the case
explicitly, exactly like `--case-id` names it here.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from uuid import UUID

import structlog

from app.core.config import Settings, get_settings
from app.modules.graph.integration_projector import GraphUpdateRunSummary, replay_graph_updates
from app.modules.graph.integration_repository import GraphCorrelationIntegrationRepository
from app.modules.graph.integration_repository import create_engine as create_postgres_engine
from app.modules.graph.intelligence.pipeline import run_case_correlation_pass
from app.modules.graph.intelligence.projection import make_correlation_projection_handler
from app.modules.graph.repository import Neo4jGraphRepository, create_driver

logger = structlog.get_logger(__name__)

_DEFAULT_LEASE_SECONDS = 120
_DEFAULT_BATCH_SIZE = 25
_DEFAULT_POLL_INTERVAL_SECONDS = 5.0


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


async def replay_once(settings: Settings) -> GraphUpdateRunSummary:
    """Claim and attempt one bounded batch of `graph_update_events`, then close both connections."""
    postgres_engine = create_postgres_engine(settings)
    repository = GraphCorrelationIntegrationRepository(postgres_engine)
    neo4j_driver = create_driver(settings)
    graph = Neo4jGraphRepository(neo4j_driver)
    try:
        handler = make_correlation_projection_handler(graph)
        return await replay_graph_updates(
            repository,
            handler,
            lease_seconds=_DEFAULT_LEASE_SECONDS,
            batch_size=_DEFAULT_BATCH_SIZE,
            now=datetime.now(UTC),
        )
    finally:
        await repository.close()
        await graph.close()


async def _interruptible_wait(shutdown_event: asyncio.Event, seconds: float) -> None:
    with suppress(TimeoutError):
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)


async def replay_loop(settings: Settings, *, shutdown_event: asyncio.Event) -> str:
    """Continuously replay `graph_update_events` batches until `shutdown_event` is set.

    Mirrors `graph.worker.run_loop`'s shape (claim-again-immediately on real
    work, sleep on an empty batch, always drain the current batch before
    honoring shutdown) without duplicating its exponential-backoff/failure-
    ceiling machinery -- a Neo4j outage already has no retry ceiling by
    design in this outbox (see `docs/architecture/phase-5-integration.md`),
    so there is nothing here for a backoff cutoff to protect against beyond
    what `replay_graph_updates` itself already handles per event.
    """
    postgres_engine = create_postgres_engine(settings)
    repository = GraphCorrelationIntegrationRepository(postgres_engine)
    neo4j_driver = create_driver(settings)
    graph = Neo4jGraphRepository(neo4j_driver)
    handler = make_correlation_projection_handler(graph)
    try:
        while not shutdown_event.is_set():
            summary = await replay_graph_updates(
                repository,
                handler,
                lease_seconds=_DEFAULT_LEASE_SECONDS,
                batch_size=_DEFAULT_BATCH_SIZE,
                now=datetime.now(UTC),
            )
            logger.info(
                "graph.intelligence_worker.replay_batch_processed",
                claimed=summary.claimed,
                succeeded=summary.succeeded,
                retrying=summary.retrying,
                failed=summary.failed,
            )
            if summary.claimed == 0:
                await _interruptible_wait(shutdown_event, _DEFAULT_POLL_INTERVAL_SECONDS)
    finally:
        await repository.close()
        await graph.close()
    return "shutdown_requested"


async def _replay_loop_with_signal_handling(settings: Settings) -> str:
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _handle(sig: signal.Signals) -> None:
        logger.info("graph.intelligence_worker.loop_shutdown_signal_received", signal=sig.name)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle, sig)

    return await replay_loop(settings, shutdown_event=shutdown_event)


async def generate_once(settings: Settings, case_id: UUID) -> bool:
    """Run one retrieval -> scoring -> correlation -> submit pass for one case.

    Returns `True` if a correlation submission was built and reached
    Nipun's durable seam; `False` if this case currently has too little
    retrieval-eligible evidence to produce one -- both are normal,
    successful CLI outcomes, never an error.
    """
    postgres_engine = create_postgres_engine(settings)
    repository = GraphCorrelationIntegrationRepository(postgres_engine)
    try:
        receipt = await run_case_correlation_pass(repository, postgres_engine, case_id)
    finally:
        await repository.close()
    if receipt is None:
        logger.info("graph.intelligence_worker.generate_no_candidates", case_id=str(case_id))
        return False
    logger.info(
        "graph.intelligence_worker.generate_submitted",
        case_id=str(case_id),
        correlation_id=str(receipt.correlation.correlation_id),
        replayed=receipt.replayed,
    )
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.modules.graph.intelligence_worker",
        description=(
            "Replay queued correlation.upserted.v1 graph-update events into Neo4j, "
            "or generate one case's candidate correlations from canonical observations."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--replay-once", action="store_true", help="Replay one bounded batch, then exit."
    )
    mode.add_argument(
        "--replay-loop", action="store_true", help="Replay continuously until SIGINT/SIGTERM."
    )
    mode.add_argument(
        "--generate",
        action="store_true",
        help="Run one retrieval/scoring/correlation pass for --case-id, then exit.",
    )
    parser.add_argument(
        "--case-id", type=str, default=None, help="Required with --generate: the case to process."
    )
    args = parser.parse_args(argv)

    if args.generate and not args.case_id:
        parser.error("--generate requires --case-id")

    _configure_logging()
    settings = get_settings()

    if args.replay_once:
        try:
            summary = asyncio.run(replay_once(settings))
        except Exception as exc:  # a startup/connection failure, not a per-event outcome
            logger.error("graph.intelligence_worker.replay_run_failed", exc_type=type(exc).__name__)
            return 1
        logger.info(
            "graph.intelligence_worker.replay_run_completed",
            claimed=summary.claimed,
            succeeded=summary.succeeded,
            retrying=summary.retrying,
            failed=summary.failed,
        )
        return 1 if summary.failed > 0 else 0

    if args.replay_loop:
        try:
            asyncio.run(_replay_loop_with_signal_handling(settings))
        except Exception as exc:  # a startup/connection failure, not a per-event outcome
            logger.error(
                "graph.intelligence_worker.replay_loop_failed", exc_type=type(exc).__name__
            )
            return 1
        return 0

    try:
        case_id = UUID(args.case_id)
    except ValueError:
        parser.error("--case-id must be a valid UUID")
        return 2  # pragma: no cover - argparse.error already exits

    try:
        asyncio.run(generate_once(settings, case_id))
    except Exception as exc:  # a startup/connection/validation failure
        logger.error("graph.intelligence_worker.generate_run_failed", exc_type=type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
