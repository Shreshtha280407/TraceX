"""Scenarios 20-21: the retry helper retries only allowed idempotent operations."""

from __future__ import annotations

import pytest

from app.modules.access_control.errors import RetryNotAllowedError
from app.modules.access_control.retry import NEVER_RETRY_OPERATIONS, retry_async


class _FlakyOperation:
    def __init__(self, failures: int, exception: type[Exception] = ConnectionError) -> None:
        self.failures = failures
        self.exception = exception
        self.call_count = 0

    async def __call__(self) -> str:
        self.call_count += 1
        if self.call_count <= self.failures:
            raise self.exception("transient")
        return "ok"


async def _no_sleep(_delay: float) -> None:
    return None


# --- Scenario 20: retries allowed idempotent transient operations -----------


async def test_retries_a_transient_failure_and_eventually_succeeds() -> None:
    op = _FlakyOperation(failures=2)
    result = await retry_async(
        op, operation_name="graph_read_query", max_attempts=3, sleep=_no_sleep, jitter=lambda: 0.0
    )
    assert result == "ok"
    assert op.call_count == 3


async def test_gives_up_after_max_attempts_and_raises_the_last_error() -> None:
    op = _FlakyOperation(failures=10)
    with pytest.raises(ConnectionError):
        await retry_async(
            op,
            operation_name="graph_read_query",
            max_attempts=3,
            sleep=_no_sleep,
            jitter=lambda: 0.0,
        )
    assert op.call_count == 3


async def test_non_retryable_exception_propagates_on_first_attempt() -> None:
    op = _FlakyOperation(failures=10, exception=ValueError)
    with pytest.raises(ValueError):
        await retry_async(
            op,
            operation_name="graph_read_query",
            max_attempts=5,
            sleep=_no_sleep,
            retryable_exceptions=(ConnectionError,),
        )
    assert op.call_count == 1


async def test_backoff_delay_is_bounded_and_uses_injected_sleep() -> None:
    delays: list[float] = []

    async def _record_sleep(delay: float) -> None:
        delays.append(delay)

    op = _FlakyOperation(failures=2)
    await retry_async(
        op,
        operation_name="graph_read_query",
        max_attempts=3,
        base_delay_seconds=0.1,
        max_delay_seconds=1.0,
        sleep=_record_sleep,
        jitter=lambda: 0.0,
    )
    assert delays == [0.1, 0.2]
    assert all(d <= 1.0 for d in delays)


async def test_jitter_is_clamped_and_cannot_make_backoff_unbounded() -> None:
    delays: list[float] = []

    async def _record_sleep(delay: float) -> None:
        delays.append(delay)

    await retry_async(
        _FlakyOperation(failures=1),
        operation_name="graph_read_query",
        max_attempts=2,
        base_delay_seconds=1.0,
        max_delay_seconds=1.0,
        sleep=_record_sleep,
        jitter=lambda: 1_000_000.0,
    )
    assert delays == [1.1]


async def test_unsafe_operation_name_is_rejected_before_work() -> None:
    op = _FlakyOperation(failures=0)
    with pytest.raises(ValueError, match="safe identifier"):
        await retry_async(op, operation_name="graph read\npassword=value")
    assert op.call_count == 0


async def test_retry_exhaustion_log_contains_type_not_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []

    class _Logger:
        def warning(self, event: str, **values: object) -> None:
            events.append((event, values))

        def error(self, event: str, **values: object) -> None:
            events.append((event, values))

    monkeypatch.setattr("app.modules.access_control.retry.logger", _Logger())
    op = _FlakyOperation(failures=2)
    with pytest.raises(ConnectionError, match="transient"):
        await retry_async(
            op,
            operation_name="graph_read_query",
            max_attempts=2,
            sleep=_no_sleep,
            jitter=lambda: 0.0,
        )
    assert [event for event, _ in events] == [
        "operation.retry_scheduled",
        "operation.retry_exhausted",
    ]
    assert "transient" not in repr(events)
    assert all(values["exc_type"] == "ConnectionError" for _, values in events)


# --- Scenario 21: never retries token/session/auth operations --------------


@pytest.mark.parametrize("operation_name", sorted(NEVER_RETRY_OPERATIONS))
async def test_never_retries_named_non_idempotent_operations(operation_name: str) -> None:
    op = _FlakyOperation(failures=0)  # would succeed immediately if ever called
    with pytest.raises(RetryNotAllowedError):
        await retry_async(op, operation_name=operation_name, sleep=_no_sleep)
    assert op.call_count == 0, "operation must never be invoked, not even once"


def test_never_retry_set_covers_every_named_security_action() -> None:
    expected = {
        "login",
        "register",
        "password_change",
        "token_rotation",
        "session_revocation",
        "review_decision",
        "evidence_write",
    }
    assert expected == NEVER_RETRY_OPERATIONS
