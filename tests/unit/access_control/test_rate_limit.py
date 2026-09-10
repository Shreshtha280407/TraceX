"""Rate limiter unit tests: fixed-window behavior and the Redis fail-closed policy."""

from __future__ import annotations

from app.modules.access_control.rate_limit import (
    RATE_LIMIT_WINDOW_SECONDS,
    InMemoryRateLimiter,
    RedisRateLimiter,
    hash_rate_limit_key,
)


def test_hash_rate_limit_key_never_contains_the_raw_identifier() -> None:
    key = hash_rate_limit_key("login", "someone@example.test")
    assert "someone@example.test" not in key
    assert key.startswith("ratelimit:login:")


def test_hash_rate_limit_key_differs_by_purpose() -> None:
    login_key = hash_rate_limit_key("login", "someone@example.test")
    refresh_key = hash_rate_limit_key("refresh", "someone@example.test")
    assert login_key != refresh_key


async def test_in_memory_limiter_allows_up_to_the_limit_then_denies() -> None:
    clock = iter([0.0] * 10).__next__
    limiter = InMemoryRateLimiter(clock=clock)
    key = "k"
    results = [await limiter.check_and_increment(key, limit=3) for _ in range(5)]
    assert results == [True, True, True, False, False]


async def test_in_memory_limiter_resets_in_a_new_window() -> None:
    times = iter([0.0, 0.0, RATE_LIMIT_WINDOW_SECONDS + 1.0])
    limiter = InMemoryRateLimiter(clock=lambda: next(times))
    key = "k"
    assert await limiter.check_and_increment(key, limit=1) is True
    assert await limiter.check_and_increment(key, limit=1) is False
    assert await limiter.check_and_increment(key, limit=1) is True  # new window


async def test_in_memory_limiter_keys_are_independent() -> None:
    limiter = InMemoryRateLimiter(clock=lambda: 0.0)
    assert await limiter.check_and_increment("a", limit=1) is True
    assert await limiter.check_and_increment("b", limit=1) is True
    assert await limiter.check_and_increment("a", limit=1) is False


class _RaisingRedisClient:
    """Fakes a Redis client whose every call fails, as if the connection were down."""

    async def incr(self, key: str) -> int:
        import redis.exceptions

        raise redis.exceptions.ConnectionError("simulated redis outage")

    async def expire(self, key: str, seconds: int) -> None:
        raise AssertionError("expire should never be reached if incr already failed")


async def test_redis_limiter_fails_closed_on_outage() -> None:
    limiter = RedisRateLimiter(_RaisingRedisClient())  # type: ignore[arg-type]
    allowed = await limiter.check_and_increment("k", limit=1000)
    assert allowed is False


class _WorkingRedisClient:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.expired: list[tuple[str, int]] = []

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> None:
        self.expired.append((key, seconds))


async def test_redis_limiter_allows_within_limit_and_sets_expiry_once() -> None:
    client = _WorkingRedisClient()
    limiter = RedisRateLimiter(client)  # type: ignore[arg-type]
    assert await limiter.check_and_increment("k", limit=2) is True
    assert await limiter.check_and_increment("k", limit=2) is True
    assert await limiter.check_and_increment("k", limit=2) is False
    assert client.expired == [("k", RATE_LIMIT_WINDOW_SECONDS)]
