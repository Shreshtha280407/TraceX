"""Scenario 6: the Redis-backed rate limiter, against a live Redis instance."""

from __future__ import annotations

import uuid
from typing import Any

from app.modules.access_control.rate_limit import RATE_LIMIT_WINDOW_SECONDS, RedisRateLimiter


async def test_redis_rate_limiter_enforces_the_limit_live(redis_client: Any) -> None:
    limiter = RedisRateLimiter(redis_client)
    key = f"itest:ratelimit:{uuid.uuid4().hex}"
    try:
        results = [await limiter.check_and_increment(key, limit=3) for _ in range(5)]
        assert results == [True, True, True, False, False]

        ttl = await redis_client.ttl(key)
        assert 0 < ttl <= RATE_LIMIT_WINDOW_SECONDS
    finally:
        await redis_client.delete(key)


async def test_redis_rate_limiter_keys_are_independent_live(redis_client: Any) -> None:
    limiter = RedisRateLimiter(redis_client)
    key_a = f"itest:ratelimit:{uuid.uuid4().hex}"
    key_b = f"itest:ratelimit:{uuid.uuid4().hex}"
    try:
        assert await limiter.check_and_increment(key_a, limit=1) is True
        assert await limiter.check_and_increment(key_b, limit=1) is True
        assert await limiter.check_and_increment(key_a, limit=1) is False
    finally:
        await redis_client.delete(key_a, key_b)
