"""The graph-projection worker entry point: a bounded-batch `--once` CLI, plus a
continuous `--loop` mode.

    uv run python -m app.modules.graph.worker --once
    uv run python -m app.modules.graph.worker --loop

`--once` claims up to `GRAPH_PROJECTION_BATCH_SIZE` durable
`graph_projection_jobs` rows, attempts to project each one's canonical
`ObservationV1` into Neo4j, then exits -- a cron/systemd timer or an
operator's own script can invoke it repeatedly. `--loop` (Phase 2
closeout) runs that same batch logic continuously in-process instead,
with a configurable idle-poll interval, bounded exponential backoff on
repeated failure, a maximum-consecutive-failures cutoff, and graceful
SIGINT/SIGTERM shutdown -- see `run_loop`'s docstring for the full policy.
Both modes share the exact same `run_batch` (`projector.py`) claim/project/
mark-outcome logic; `--loop` only adds the outer continuous-operation
wrapper around it.

Unlike the extraction workers (`structured_processing.worker`,
`communication_processing.worker`, `media_processing.worker`), this one
talks to PostgreSQL and Neo4j directly (`GraphProjectionOutboxRepository`,
`Neo4jGraphRepository`) rather than through the internal worker HTTP API --
it is a backend-owned internal service, not an extractor worker bound by
the "workers must not require direct PostgreSQL/Neo4j/MinIO credentials"
rule in `CLAUDE.md` (that rule targets untrusted, arbitrary extraction
code; this module is authored and operated by the same team as the backend
itself).

Exit code for `--once`: `0` if every claimed job either succeeded or was
left safely retryable/deferred for a future run; `1` if any job reached a
terminal `failed` state (or nothing could be attempted due to a startup/
connection problem) -- so a cron/systemd wrapper can alert on it, without
treating an ordinary "Neo4j was briefly unavailable, will retry" run as an
error. Exit code for `--loop`: `0` only on a clean SIGINT/SIGTERM shutdown;
`1` if the loop stopped itself after `max_consecutive_failures` -- a
worsening problem an operator should be paged for, not silently retried
forever.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
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
            renew_interval_seconds=settings.graph_projection_renew_interval_seconds,
        )
    finally:
        await outbox.close()
        await graph.close()


@dataclass(frozen=True)
class RunLoopSummary:
    """What one `run_loop` call did before stopping -- for the CLI's exit code and for tests."""

    iterations: int
    batches_with_work: int
    total_claimed: int
    total_succeeded: int
    total_failed: int
    #: `"shutdown_requested"` (a clean stop, exit `0`) or
    #: `"max_consecutive_failures"` (a worsening problem, exit `1`).
    stopped_reason: str


async def _interruptible_wait(shutdown_event: asyncio.Event, seconds: float) -> None:
    """Sleep up to `seconds`, waking immediately if `shutdown_event` is set first."""
    with suppress(TimeoutError):
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)


async def run_loop(
    settings: Settings,
    *,
    shutdown_event: asyncio.Event,
    poll_interval_seconds: float | None = None,
    max_backoff_seconds: float | None = None,
    max_consecutive_failures: int | None = None,
) -> RunLoopSummary:
    """Continuously claim-and-project batches until `shutdown_event` is set or too
    many consecutive failures occur.

    Unlike `run_once` (a fresh PostgreSQL engine + Neo4j driver per
    invocation, appropriate for a one-shot CLI process), this builds both
    connections once and reuses them for the whole loop's lifetime --
    appropriate for a long-running process; both are always released in a
    `finally` when the loop stops, however it stops.

    `shutdown_event` is checked *before* every `run_batch` call, never
    mid-batch -- once set, this loop claims no further batches; whatever
    batch is already in flight always finishes (every claimed job in it
    terminates in `mark_succeeded`/`mark_retryable_failure`/`mark_failed`,
    `run_batch`'s own unconditional contract) before control returns here,
    so no partially-claimed job is ever abandoned by a shutdown request. A
    batch that claimed at least one job loops again immediately (more work
    may be queued); an empty batch sleeps `poll_interval_seconds` (via
    `shutdown_event.wait`, so a shutdown request wakes it immediately
    rather than after the full interval). An exception escaping `run_batch`
    (Neo4j/PostgreSQL connection failure at the driver level, not a
    per-job outcome -- those are all handled inside `run_batch` itself)
    increments a consecutive-failure counter and sleeps a bounded
    exponential backoff (`poll_interval_seconds * 2**consecutive_failures`,
    capped at `max_backoff_seconds`); reaching `max_consecutive_failures`
    stops the loop entirely -- a real, worsening problem, not something to
    retry forever silently (see `main`'s exit-code mapping). Explicit
    `None` arguments (the default) fall back to `Settings`; tests can pass
    small explicit values to run this deterministically and fast.
    """
    poll_interval = (
        poll_interval_seconds
        if poll_interval_seconds is not None
        else settings.graph_projector_poll_interval_seconds
    )
    backoff_cap = (
        max_backoff_seconds
        if max_backoff_seconds is not None
        else settings.graph_projector_max_backoff_seconds
    )
    failure_limit = (
        max_consecutive_failures
        if max_consecutive_failures is not None
        else settings.graph_projector_max_consecutive_failures
    )

    postgres_engine = create_postgres_engine(settings)
    outbox = GraphProjectionOutboxRepository(postgres_engine)
    neo4j_driver = create_driver(settings)
    graph = Neo4jGraphRepository(neo4j_driver)

    iterations = 0
    batches_with_work = 0
    total_claimed = total_succeeded = total_failed = 0
    consecutive_failures = 0
    try:
        while not shutdown_event.is_set():
            iterations += 1
            try:
                summary = await run_batch(
                    outbox,
                    graph,
                    now=datetime.now(UTC),
                    lease_seconds=settings.graph_projection_lease_seconds,
                    batch_size=settings.graph_projection_batch_size,
                    renew_interval_seconds=settings.graph_projection_renew_interval_seconds,
                )
            except Exception as exc:  # a connection/startup failure, not a per-job outcome
                consecutive_failures += 1
                logger.error(
                    "graph.worker.loop_iteration_failed",
                    exc_type=type(exc).__name__,
                    consecutive_failures=consecutive_failures,
                )
                if consecutive_failures >= failure_limit:
                    return RunLoopSummary(
                        iterations,
                        batches_with_work,
                        total_claimed,
                        total_succeeded,
                        total_failed,
                        "max_consecutive_failures",
                    )
                backoff = min(poll_interval * (2**consecutive_failures), backoff_cap)
                await _interruptible_wait(shutdown_event, backoff)
                continue

            consecutive_failures = 0
            total_claimed += summary.claimed
            total_succeeded += summary.succeeded
            total_failed += summary.failed
            if summary.claimed > 0:
                batches_with_work += 1
                logger.info(
                    "graph.worker.loop_batch_processed",
                    claimed=summary.claimed,
                    succeeded=summary.succeeded,
                    failed=summary.failed,
                    retrying=summary.retrying,
                    deferred=summary.deferred,
                )
                continue
            await _interruptible_wait(shutdown_event, poll_interval)
    finally:
        await outbox.close()
        await graph.close()

    return RunLoopSummary(
        iterations, batches_with_work, total_claimed, total_succeeded, total_failed,
        "shutdown_requested",
    )  # fmt: skip


async def _run_loop_with_signal_handling(settings: Settings) -> RunLoopSummary:
    """Wire real SIGINT/SIGTERM to `run_loop`'s `shutdown_event` for the real CLI (`main`).

    `asyncio`-native signal handling (`loop.add_signal_handler`), not raw
    `signal.signal` -- correct and safe to combine with `asyncio.Event`
    from within the running event loop, unlike a signal handler installed
    the synchronous way while a coroutine is suspended mid-await.
    """
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _handle(sig: signal.Signals) -> None:
        logger.info("graph.worker.loop_shutdown_signal_received", signal=sig.name)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle, sig)

    return await run_loop(settings, shutdown_event=shutdown_event)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point:

    uv run python -m app.modules.graph.worker --once
    uv run python -m app.modules.graph.worker --loop
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.modules.graph.worker",
        description=(
            "Claim and project durable graph-projection jobs. --once attempts one "
            "bounded batch then exits; --loop runs continuously until SIGINT/SIGTERM."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--once", action="store_true", help="Run exactly one claim-and-project batch, then exit."
    )
    mode.add_argument(
        "--loop", action="store_true", help="Run continuously until SIGINT/SIGTERM, then exit."
    )
    args = parser.parse_args(argv)

    _configure_logging()
    settings = get_settings()

    if args.once:
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

    try:
        loop_summary = asyncio.run(_run_loop_with_signal_handling(settings))
    except Exception as exc:  # a startup/connection failure, not a per-job outcome
        logger.error("graph.worker.loop_failed", exc_type=type(exc).__name__)
        return 1

    logger.info(
        "graph.worker.loop_completed",
        iterations=loop_summary.iterations,
        batches_with_work=loop_summary.batches_with_work,
        total_claimed=loop_summary.total_claimed,
        total_succeeded=loop_summary.total_succeeded,
        total_failed=loop_summary.total_failed,
        stopped_reason=loop_summary.stopped_reason,
    )
    return 0 if loop_summary.stopped_reason == "shutdown_requested" else 1


if __name__ == "__main__":
    sys.exit(main())
