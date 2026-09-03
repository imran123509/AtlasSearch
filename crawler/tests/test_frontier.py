from __future__ import annotations

import pytest

from atlas_crawler.frontier import Frontier


@pytest.fixture
def frontier(redis, cfg):
    return Frontier(redis, cfg)


async def test_add_then_lease_roundtrip(frontier):
    assert await frontier.add("https://example.com/a") is True
    tasks = await frontier.lease(1)
    assert [t.url for t in tasks] == ["https://example.com/a"]
    assert tasks[0].lease_token


async def test_urls_are_canonicalised_on_add(frontier):
    await frontier.add("HTTPS://Example.COM/a?utm_source=x#frag")
    tasks = await frontier.lease(1)
    assert tasks[0].url == "https://example.com/a"


async def test_duplicate_url_in_queue_is_rejected(frontier):
    assert await frontier.add("https://example.com/a") is True
    assert await frontier.add("https://example.com/a") is False


async def test_one_url_per_host_per_lease_call(frontier):
    """Invariant: a host is rescheduled after each URL, so one lease round
    cannot hand two URLs of the same host to two fetchers."""
    for i in range(5):
        await frontier.add(f"https://example.com/p{i}")
    tasks = await frontier.lease(5)
    assert len(tasks) == 1


async def test_multiple_hosts_lease_in_parallel(frontier):
    for host in ("a.test", "b.test", "c.test"):
        await frontier.add(f"https://{host}/p")
    tasks = await frontier.lease(3)
    assert len({t.url for t in tasks}) == 3


async def test_drained_host_leaves_the_due_heap(frontier, redis):
    """Invariant 2: an empty back queue must not idle a fetcher."""
    await frontier.add("https://example.com/only")
    await frontier.lease(1)
    assert await redis.zcard(frontier._heap) == 0


async def test_requeue_returns_the_url(frontier):
    await frontier.add("https://example.com/a")
    task = (await frontier.lease(1))[0]
    await frontier.requeue(task, delay_s=0)
    assert [t.url for t in await frontier.lease(1)] == ["https://example.com/a"]


async def test_requeue_with_delay_defers_the_url(frontier):
    await frontier.add("https://example.com/a")
    task = (await frontier.lease(1))[0]
    await frontier.requeue(task, delay_s=60)
    assert await frontier.lease(1) == []


async def test_release_clears_the_lease(frontier, redis):
    await frontier.add("https://example.com/a")
    task = (await frontier.lease(1))[0]
    assert await redis.get(frontier._lease(task.url)) is not None
    await frontier.release(task)
    assert await redis.get(frontier._lease(task.url)) is None


async def test_release_does_not_steal_a_reissued_lease(frontier, redis):
    """After a lease expires and is reissued, the old holder must not free it."""
    await frontier.add("https://example.com/a")
    task = (await frontier.lease(1))[0]
    await redis.set(frontier._lease(task.url), "someone-elses-token")
    await frontier.release(task)
    assert await redis.get(frontier._lease(task.url)) == b"someone-elses-token"


async def test_validators_survive_a_requeue(frontier):
    await frontier.add("https://example.com/a")
    await frontier.store_validators(
        "https://example.com/a", etag='W/"v1"', last_modified="Mon, 1 Sep 2025 00:00:00 GMT"
    )
    task = (await frontier.lease(1))[0]
    assert task.etag == 'W/"v1"'
    assert task.last_modified == "Mon, 1 Sep 2025 00:00:00 GMT"


async def test_site_budget_caps_a_trap(frontier, redis):
    """Per-site budget stops a URL generator without ever identifying it as one."""
    await redis.set("fr:auth:trap.test", "0")
    accepted = 0
    for i in range(2000):
        if await frontier.add(f"https://trap.test/calendar/2026/{i}"):
            accepted += 1
    assert accepted < 2000, "the trap consumed an unbounded share of the frontier"
    budget = await frontier.site_budget("trap.test")
    assert accepted <= budget


async def test_higher_authority_earns_a_larger_budget(frontier, redis):
    await redis.set("fr:auth:small.test", "0")
    await redis.set("fr:auth:big.test", "0.5")
    assert await frontier.site_budget("big.test") > await frontier.site_budget("small.test")


async def test_stats_reports_scheduled_and_due(frontier):
    await frontier.add("https://a.test/p")
    await frontier.add("https://b.test/p")
    stats = await frontier.stats()
    assert stats["hosts_scheduled"] == 2
    assert stats["hosts_due"] == 2


async def test_empty_frontier_leases_nothing(frontier):
    assert await frontier.lease(5) == []
