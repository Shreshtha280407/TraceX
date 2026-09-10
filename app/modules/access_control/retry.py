"""Bounded retry utility for explicitly idempotent, transient-failure-prone operations.

A foundation for later service adapters -- never used to paper over a real
database correctness bug, and never permitted for any of the
non-idempotent security-sensitive operations named in
`NEVER_RETRY_OPERATIONS` below.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from app.modules.access_control.errors import RetryNotAllowedError

#: Operation names this helper must never retry, regardless of the
#: exception raised. Retrying any of these risks duplicating a
#: side-effecting security action -- e.g. a second token-rotation call
#: after the first one actually already rotated the session, or a second
#: review-decision write. Enforced here defensively, in addition to every
#: caller already knowing not to pass one of these.
NEVER_RETRY_OPERATIONS: frozenset[str] = frozenset(
    {
        "login",
        "register",
        "password_change",
        "token_rotation",
        "session_revocation",
        "review_decision",
        "evidence_write",
    }
)

DEFAULT_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (TimeoutError, ConnectionError)


async def retry_async[T](
    operation: Callable[[], Awaitable[T]],
    *,
    operation_name: str,
    max_attempts: int = 3,
    base_delay_seconds: float = 0.1,
    max_delay_seconds: float = 2.0,
    retryable_exceptions: tuple[type[Exception], ...] = DEFAULT_RETRYABLE_EXCEPTIONS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[], float] = random.random,
) -> T:
    """Run `operation` with bounded exponential backoff and jitter.

    For idempotent, transient-failure-prone work only. Raises
    `RetryNotAllowedError` immediately -- without calling `operation` even
    once -- if `operation_name` is in `NEVER_RETRY_OPERATIONS`. This check
    happens before any attempt, so a caller can never accidentally retry a
    non-idempotent security action by passing the wrong name: it fails
    closed, not open.

    Only exceptions matching `retryable_exceptions` trigger a retry; any
    other exception propagates immediately. Backoff is
    `min(max_delay_seconds, base_delay_seconds * 2**(attempt-1))`, plus up
    to 10% bounded jitter on top -- never negative, never unbounded.
    `sleep`/`jitter` are injectable so tests never actually wait.
    """
    if operation_name in NEVER_RETRY_OPERATIONS:
        raise RetryNotAllowedError(f"operation {operation_name!r} must never be retried")
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()
        except retryable_exceptions:
            if attempt == max_attempts:
                raise
            delay = min(max_delay_seconds, base_delay_seconds * (2 ** (attempt - 1)))
            delay += delay * 0.1 * jitter()
            await sleep(delay)
    raise AssertionError("unreachable: loop always returns or raises")
