"""`graph.worker.run_loop`'s own poll/backoff/shutdown control flow, with
`run_batch` monkeypatched so this needs no real PostgreSQL/Neo4j connection
-- `create_engine`/`create_driver` build lazy client objects that never
connect until first used, so constructing (and later closing) them requires
only a syntactically valid configured DSN/URI, not a reachable one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from app.core.config import get_settings
from app.modules.graph import worker as worker_module
from app.modules.graph.projector import ProjectorRunSummary


def _make_run_batch(
    outcomes: list[ProjectorRunSummary | Exception],
) -> Callable[..., object]:
    calls = {"count": 0}

    async def _fake_run_batch(*_args: object, **_kwargs: object) -> ProjectorRunSummary:
        outcome = outcomes[calls["count"] % len(outcomes)]
        calls["count"] += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    _fake_run_batch.calls = calls  # type: ignore[attr-defined]
    return _fake_run_batch


_EMPTY_BATCH = ProjectorRunSummary(claimed=0)


async def test_run_loop_stops_immediately_when_shutdown_already_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _make_run_batch([_EMPTY_BATCH])
    monkeypatch.setattr(worker_module, "run_batch", fake)
    event = asyncio.Event()
    event.set()
    summary = await worker_module.run_loop(
        get_settings(), shutdown_event=event, poll_interval_seconds=0.01
    )
    assert summary.iterations == 0
    assert summary.stopped_reason == "shutdown_requested"
    assert fake.calls["count"] == 0  # type: ignore[attr-defined]


async def test_run_loop_claims_no_further_batch_once_shutdown_is_requested_mid_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = asyncio.Event()
    calls = {"count": 0}

    async def _fake_run_batch(*_args: object, **_kwargs: object) -> ProjectorRunSummary:
        calls["count"] += 1
        if calls["count"] >= 2:
            event.set()
        return _EMPTY_BATCH

    monkeypatch.setattr(worker_module, "run_batch", _fake_run_batch)
    summary = await worker_module.run_loop(
        get_settings(), shutdown_event=event, poll_interval_seconds=0.01
    )
    assert summary.stopped_reason == "shutdown_requested"
    assert calls["count"] == 2  # never claims a 3rd batch after the 2nd sets shutdown


async def test_run_loop_stops_after_max_consecutive_failures_with_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _make_run_batch([RuntimeError("neo4j connection failed")])
    monkeypatch.setattr(worker_module, "run_batch", fake)
    event = asyncio.Event()
    loop = asyncio.get_event_loop()
    started = loop.time()
    summary = await worker_module.run_loop(
        get_settings(),
        shutdown_event=event,
        poll_interval_seconds=0.01,
        max_backoff_seconds=0.05,
        max_consecutive_failures=3,
    )
    elapsed = loop.time() - started
    assert summary.stopped_reason == "max_consecutive_failures"
    assert summary.total_claimed == 0
    assert elapsed >= 0.05  # backoff after failures 1, 2: 0.02 + 0.04 = 0.06s minimum


async def test_run_loop_resets_failure_counter_after_a_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes: list[ProjectorRunSummary | Exception] = [
        RuntimeError("transient"),
        RuntimeError("transient"),
        _EMPTY_BATCH,  # resets the counter
        RuntimeError("transient"),
        RuntimeError("transient"),
        _EMPTY_BATCH,
    ]
    event = asyncio.Event()
    calls = {"count": 0}

    async def _wrapped(*_args: object, **_kwargs: object) -> ProjectorRunSummary:
        outcome = outcomes[calls["count"]]
        calls["count"] += 1
        if calls["count"] >= 6:
            event.set()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(worker_module, "run_batch", _wrapped)
    summary = await worker_module.run_loop(
        get_settings(),
        shutdown_event=event,
        poll_interval_seconds=0.01,
        max_backoff_seconds=0.02,
        max_consecutive_failures=3,  # would've stopped at failure 3 without the reset
    )
    assert summary.stopped_reason == "shutdown_requested"


async def test_run_loop_accumulates_totals_and_loops_again_immediately_when_work_was_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_with_work = ProjectorRunSummary(claimed=2, succeeded=2, failed=0, retrying=0, deferred=0)
    outcomes: list[ProjectorRunSummary | Exception] = [
        batch_with_work,
        batch_with_work,
        _EMPTY_BATCH,
    ]
    event = asyncio.Event()
    calls = {"count": 0}

    async def _fake_run_batch(*_args: object, **_kwargs: object) -> ProjectorRunSummary:
        outcome = outcomes[calls["count"]]
        calls["count"] += 1
        if calls["count"] >= 3:
            event.set()
        return outcome  # type: ignore[return-value]

    monkeypatch.setattr(worker_module, "run_batch", _fake_run_batch)
    summary = await worker_module.run_loop(
        get_settings(),
        shutdown_event=event,
        poll_interval_seconds=5.0,  # would make the test slow if actually waited on
    )
    assert summary.batches_with_work == 2
    assert summary.total_claimed == 4
    assert summary.total_succeeded == 4
