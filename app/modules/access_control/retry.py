"""Bounded retry utility for explicitly idempotent, transient-failure-prone operations.

A foundation for later service adapters -- never used to paper over a real
database correctness bug, and never permitted for any of the
non-idempotent security-sensitive operations named in
`NEVER_RETRY_OPERATIONS` below.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Awaitable, Callable

import structlog

from app.core.errors import get_request_id
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
_SAFE_OPERATION_NAME = re.compile(r"^[a-z0-9_.-]{1,64}$")
logger = structlog.get_logger(__name__)


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
    if not _SAFE_OPERATION_NAME.fullmatch(operation_name):
        raise ValueError("operation_name must be a bounded safe identifier")
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if base_delay_seconds < 0 or max_delay_seconds < 0:
        raise ValueError("retry delays must be >= 0")

    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()
        except retryable_exceptions as exc:
            if attempt == max_attempts:
                logger.error(
                    "operation.retry_exhausted",
                    request_id=get_request_id() or None,
                    operation_name=operation_name,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    exc_type=type(exc).__name__,
                )
                raise
            delay = min(max_delay_seconds, base_delay_seconds * (2 ** (attempt - 1)))
            jitter_fraction = min(1.0, max(0.0, jitter()))
            delay += delay * 0.1 * jitter_fraction
            logger.warning(
                "operation.retry_scheduled",
                request_id=get_request_id() or None,
                operation_name=operation_name,
                attempt=attempt,
                max_attempts=max_attempts,
                delay_seconds=delay,
                exc_type=type(exc).__name__,
            )
            await sleep(delay)
    raise AssertionError("unreachable: loop always returns or raises")
