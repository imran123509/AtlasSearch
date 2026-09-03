from __future__ import annotations

import asyncio

import pytest

from atlas_crawler.config import PolitenessConfig
from atlas_crawler.models import HostKey
from atlas_crawler.ratelimit import PolitenessLimiter

KEY = HostKey(domain="example.com", ip="203.0.113.7")


@pytest.fixture
def limiter(redis):
    return PolitenessLimiter(
        redis,
        PolitenessConfig(
            initial_rate=1.0, burst=1, min_rate=0.1, max_rate=4.0,
            additive_increase=0.5, multiplicative_decrease=0.5, increase_after_ok=3,
        ),
    )


async def test_first_acquire_allowed_then_burst_exhausted(limiter):
    assert (await limiter.acquire(KEY)).allowed
    d = await limiter.acquire(KEY)
    assert not d.allowed
    assert d.reason == "domain"
    assert d.retry_after_s > 0


async def test_denial_does_not_deduct_from_the_other_bucket(limiter, redis):
    """The reason both buckets are checked in one script.

    If the domain bucket were deducted before the IP bucket denied, tokens would
    leak and a well-behaved host would slowly starve.
    """
    await limiter.acquire(KEY)  # drains the domain bucket

    ip_tokens_before = await redis.hget("rl:ip:203.0.113.7", "tokens")
    denied = await limiter.acquire(KEY)
    ip_tokens_after = await redis.hget("rl:ip:203.0.113.7", "tokens")

    assert not denied.allowed
    assert ip_tokens_before == ip_tokens_after


async def test_concurrent_acquires_are_serialised(limiter):
    """The read-modify-write race the Lua script exists to prevent."""
    results = await asyncio.gather(*(limiter.acquire(KEY) for _ in range(20)))
    assert sum(1 for r in results if r.allowed) == 1  # burst=1


async def test_tokens_refill_over_time(limiter):
    assert (await limiter.acquire(KEY)).allowed
    assert not (await limiter.acquire(KEY)).allowed
    await asyncio.sleep(1.1)  # rate=1/s, burst=1
    assert (await limiter.acquire(KEY)).allowed


async def test_pause_blocks_even_with_tokens(limiter):
    await limiter.pause_host("example.com", 30, reason="http 429")
    d = await limiter.acquire(KEY)
    assert not d.allowed
    assert d.reason == "paused"
    assert 0 < d.retry_after_s <= 30


async def test_pause_is_clamped_to_max(limiter):
    await limiter.pause_host("example.com", 99999)
    assert await limiter.paused_for("example.com") <= limiter.cfg.max_retry_after_seconds


class TestAIMD:
    async def test_error_halves_the_rate(self, limiter):
        assert await limiter.current_rate("example.com") == 1.0
        assert await limiter.record("example.com", signal="err") == pytest.approx(0.5)
        assert await limiter.record("example.com", signal="err") == pytest.approx(0.25)

    async def test_rate_floors_at_min(self, limiter):
        for _ in range(20):
            await limiter.record("example.com", signal="err")
        assert await limiter.current_rate("example.com") == pytest.approx(0.1)

    async def test_increase_only_after_a_healthy_streak(self, limiter):
        await limiter.record("example.com", signal="ok")
        await limiter.record("example.com", signal="ok")
        assert await limiter.current_rate("example.com") == pytest.approx(1.0)  # streak=3
        await limiter.record("example.com", signal="ok")
        assert await limiter.current_rate("example.com") == pytest.approx(1.5)

    async def test_streak_resets_on_error(self, limiter):
        await limiter.record("example.com", signal="ok")
        await limiter.record("example.com", signal="ok")
        await limiter.record("example.com", signal="err")
        await limiter.record("example.com", signal="ok")
        await limiter.record("example.com", signal="ok")
        # Only 2 into the new streak, so no increase yet.
        assert await limiter.current_rate("example.com") == pytest.approx(0.5)

    async def test_slow_backs_off_less_sharply_than_error(self, redis):
        a = PolitenessLimiter(redis, PolitenessConfig(initial_rate=1.0, multiplicative_decrease=0.5))
        slow = await a.record("slow.example", signal="slow")
        err = await a.record("err.example", signal="err")
        assert err < slow < 1.0

    async def test_rate_ceiling_clamps_down_but_never_up(self, limiter):
        assert await limiter.apply_rate_ceiling("example.com", 0.2) is True
        assert await limiter.current_rate("example.com") == pytest.approx(0.2)
        # A generous Crawl-delay must not raise a rate AIMD has not earned.
        assert await limiter.apply_rate_ceiling("example.com", 3.0) is False
        assert await limiter.current_rate("example.com") == pytest.approx(0.2)


async def test_acquire_uses_the_current_aimd_rate(limiter):
    """Rate changes must actually reach the bucket, not just the state hash."""
    await limiter.record("example.com", signal="err")  # 1.0 -> 0.5
    assert (await limiter.acquire(KEY)).allowed
    d = await limiter.acquire(KEY)
    # At 0.5/s a single token takes ~2s, versus ~1s at the initial rate.
    assert d.retry_after_s > 1.5
