"""Login/refresh rate limiting: a narrow, purpose-built fixed-window limiter.

Two implementations share one interface (`RateLimiter`): `InMemoryRateLimiter`
(deterministic, used in unit tests) and `RedisRateLimiter` (the real backend
when Redis is reachable). Neither is a general-purpose distributed job/queue
system -- this module does exactly one thing: "has this key made more than
N attempts in the last window".
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from typing import Protocol

import redis.asyncio as redis

#: Fixed window length for every rate-limited operation in this module. Not
#: separately configurable (`AUTH_LOGIN_RATE_LIMIT`/`AUTH_REFRESH_RATE_LIMIT`
#: set only the attempt *count* allowed within this window) -- one fixed,
#: documented window keeps the config surface narrow per the phase brief.
RATE_LIMIT_WINDOW_SECONDS = 60


def hash_rate_limit_key(purpose: str, identifier: str) -> str:
    """A privacy-safe, non-reversible rate-limit key.

    Never store or log the raw `identifier` (an email or client IP) --
    only this hash, scoped by `purpose` so the same email hashes
    differently for e.g. "login" vs "refresh".
    """
    digest = hashlib.sha256(f"{purpose}:{identifier}".encode()).hexdigest()
    return f"ratelimit:{purpose}:{digest}"


class RateLimiter(Protocol):
    async def check_and_increment(self, key: str, *, limit: int) -> bool:
        """Record one attempt for `key`; return `True` if still within `limit`."""
        ...


class InMemoryRateLimiter:
    """Deterministic fixed-window limiter for unit tests (and a same-process fallback).

    `clock` is injectable (defaults to `time.monotonic`) so tests can
    control elapsed "time" without a real `sleep`.

    The window is anchored to each key's own first attempt (reset only
    once `RATE_LIMIT_WINDOW_SECONDS` has actually elapsed *since that
    attempt*), matching `RedisRateLimiter`'s real semantics (`INCR` +
    `EXPIRE` counts a fresh TTL from the first increment, never from an
    absolute clock boundary). An earlier version instead floor-divided the
    clock by the window length (`int(clock() // WINDOW)`), which aligns
    window boundaries to absolute wall-clock time -- a burst of calls that
    happens to straddle one of those boundaries (regardless of how many
    attempts had already been made) silently resets the count to zero
    mid-burst, letting every attempt after the boundary go uncounted and
    never trip the limit. Anchoring to first-attempt time removes that
    coincidental reset entirely.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._buckets: dict[str, tuple[int, float]] = {}

    async def check_and_increment(self, key: str, *, limit: int) -> bool:
        now = self._clock()
        count, window_start = self._buckets.get(key, (0, now))
        if now - window_start >= RATE_LIMIT_WINDOW_SECONDS:
            count, window_start = 0, now
        count += 1
        self._buckets[key] = (count, window_start)
        return count <= limit


class RedisRateLimiter:
    """Redis-backed fixed-window limiter (`INCR` + `EXPIRE`).

    Fail-closed policy: if Redis raises (connection error, timeout, ...),
    `check_and_increment` returns `False` -- the same outcome as a normal
    over-limit hit, so the caller gets the ordinary safe `429` response
    rather than silently allowing unlimited attempts while Redis is down.
    This is a deliberate, conservative MVP choice (see
    `docs/decisions/ADR-003-authentication-and-case-scoped-access-control.md`):
    the alternative (fail-open) would let an attacker force a Redis outage
    to bypass login rate limiting entirely.
    """

    def __init__(self, client: redis.Redis) -> None:
        self._client = client

    async def check_and_increment(self, key: str, *, limit: int) -> bool:
        try:
            count = await self._client.incr(key)
            if count == 1:
                await self._client.expire(key, RATE_LIMIT_WINDOW_SECONDS)
        except redis.RedisError:
            return False
        return count <= limit
