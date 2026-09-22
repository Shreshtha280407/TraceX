"""Bounded retry/exhaustion behavior for the correlation outbox loop."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from app.core.config import get_settings
from app.modules.graph import intelligence_worker
from app.modules.graph.integration_projector import GraphUpdateRunSummary
from app.modules.graph.intelligence.evaluation import deferred_evaluation_report


async def test_replay_loop_stops_before_work_when_shutdown_is_already_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def _replay(*_args: object, **_kwargs: object) -> GraphUpdateRunSummary:
        nonlocal calls
        calls += 1
        return GraphUpdateRunSummary(claimed=0)

    monkeypatch.setattr(intelligence_worker, "replay_graph_updates", _replay)
    shutdown = asyncio.Event()
    shutdown.set()
    summary = await intelligence_worker.replay_loop(
        get_settings(), shutdown_event=shutdown, poll_interval_seconds=0.001
    )
    assert summary.stopped_reason == "shutdown_requested"
    assert summary.iterations == 0
    assert calls == 0


async def test_replay_loop_exhausts_repeated_retryable_batches_with_bounded_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def _replay(*_args: object, **_kwargs: object) -> GraphUpdateRunSummary:
        nonlocal calls
        calls += 1
        return GraphUpdateRunSummary(claimed=1, retrying=1)

    waits: list[float] = []

    async def _wait(_shutdown: asyncio.Event, seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(intelligence_worker, "replay_graph_updates", _replay)
    monkeypatch.setattr(intelligence_worker, "_interruptible_wait", _wait)
    summary = await intelligence_worker.replay_loop(
        get_settings(),
        shutdown_event=asyncio.Event(),
        poll_interval_seconds=0.5,
        max_backoff_seconds=1.5,
        max_consecutive_failures=3,
    )
    assert summary.stopped_reason == "max_consecutive_failures"
    assert summary.total_retrying == 3
    assert calls == 3
    assert waits == [1.0, 1.5]


async def test_replay_loop_exhausts_driver_failures_without_logging_error_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _replay(*_args: object, **_kwargs: object) -> GraphUpdateRunSummary:
        raise ConnectionError("neo4j://user:unsafe-value@host")

    events: list[dict[str, object]] = []

    class _Logger:
        def error(self, _event: str, **values: object) -> None:
            events.append(values)

        def info(self, _event: str, **_values: object) -> None:
            return None

    async def _wait(_shutdown: asyncio.Event, _seconds: float) -> None:
        return None

    monkeypatch.setattr(intelligence_worker, "replay_graph_updates", _replay)
    monkeypatch.setattr(intelligence_worker, "_interruptible_wait", _wait)
    monkeypatch.setattr(intelligence_worker, "logger", _Logger())
    summary = await intelligence_worker.replay_loop(
        get_settings(),
        shutdown_event=asyncio.Event(),
        max_consecutive_failures=2,
    )
    assert summary.stopped_reason == "max_consecutive_failures"
    assert len(events) == 2
    assert all(event["exc_type"] == "ConnectionError" for event in events)
    assert "unsafe-value" not in repr(events)


# --- Gap-Closure WP-7B (G1): --evaluate CLI mode -----------------------------


def test_evaluate_requires_case_id(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        intelligence_worker.main(["--evaluate"])
    assert "--evaluate requires --case-id" in capsys.readouterr().err


def test_evaluate_mode_prints_the_report_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen_case_id: UUID | None = None

    async def _fake_evaluate_once(_settings: object, case_id: UUID) -> object:
        nonlocal seen_case_id
        seen_case_id = case_id
        return deferred_evaluation_report(rules_config_hash="synthetic-hash")

    monkeypatch.setattr(intelligence_worker, "evaluate_once", _fake_evaluate_once)
    case_id = uuid4()

    exit_code = intelligence_worker.main(["--evaluate", "--case-id", str(case_id)])

    assert exit_code == 0
    assert seen_case_id == case_id
    assert '"deferred": true' in capsys.readouterr().out
