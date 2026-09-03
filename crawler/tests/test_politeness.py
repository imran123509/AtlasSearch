"""The politeness invariant, tested end-to-end through the fetcher.

features/WEB-CRAWLER.md: "Integration test asserting no two in-flight requests
share a limiter key. This is worth a test."

It is worth a test because a politeness bug does not show up as a failing
assertion anywhere else — it shows up as your crawler getting blocked, or as a
complaint from a site operator.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import respx

from atlas_crawler.config import Config, FetchConfig, PolitenessConfig
from atlas_crawler.fetcher import Fetcher
from atlas_crawler.models import CrawlTask, Outcome
from atlas_crawler.ratelimit import PolitenessLimiter
from atlas_crawler.robots import RobotsCache

from conftest import StubDNS

ALLOW_ALL = "User-agent: *\nAllow: /\n"
HOLD = 0.05  # how long each "server" holds the connection open


class IntervalRecorder:
    """Records [enter, exit] per host so overlaps can be detected."""

    def __init__(self) -> None:
        self.intervals: dict[str, list[tuple[float, float]]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        start = time.monotonic()
        # respx handlers are sync; block briefly to simulate a held connection.
        time.sleep(HOLD)
        self.intervals.setdefault(host, []).append((start, time.monotonic()))
        return httpx.Response(200, html="<html>ok</html>")

    def overlaps(self, host: str) -> list[tuple[tuple[float, float], tuple[float, float]]]:
        spans = sorted(self.intervals.get(host, []))
        return [
            (spans[i], spans[i + 1])
            for i in range(len(spans) - 1)
            if spans[i][1] > spans[i + 1][0]
        ]


def make_fetcher(client, redis, *, dns, burst=1, rate=1.0, concurrency=16) -> Fetcher:
    cfg = Config(
        user_agent="AtlasSearchTestBot/1.0 (+https://example.org/bot)",
        robots_agent="atlassearchtestbot",
        politeness=PolitenessConfig(initial_rate=rate, burst=burst),
        fetch=FetchConfig(max_concurrent=concurrency, total_timeout=5.0),
    )
    robots = RobotsCache(
        client, redis, user_agent=cfg.user_agent,
        robots_agent=cfg.robots_agent, config=cfg.robots,
    )
    return Fetcher(
        client=client, dns=dns, robots=robots,
        limiter=PolitenessLimiter(redis, cfg.politeness), config=cfg,
    )


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


@respx.mock
async def test_no_two_in_flight_requests_share_a_host(client, redis):
    """THE invariant. 16 concurrent fetches, zero overlapping requests."""
    rec = IntervalRecorder()
    respx.get("https://example.com/robots.txt").mock(httpx.Response(200, text=ALLOW_ALL))
    respx.route(host="example.com").mock(side_effect=rec.handler)

    f = make_fetcher(client, redis, dns=StubDNS(), burst=1)
    results = await asyncio.gather(
        *(f.fetch(CrawlTask(url=f"https://example.com/p{i}")) for i in range(16))
    )

    assert rec.overlaps("example.com") == [], "two requests to one host overlapped in time"
    fetched = [r for r in results if r.outcome is Outcome.FETCHED]
    deferred = [r for r in results if r.outcome is Outcome.RATE_LIMITED]
    assert len(fetched) == 1, "burst=1 must admit exactly one"
    assert len(deferred) == 15, "the rest must be deferred, not dropped"


@respx.mock
async def test_different_hosts_are_not_serialised_against_each_other(client, redis):
    """The key is per-host. A global limiter would be politeness by accident."""
    rec = IntervalRecorder()
    for host in ("a.test", "b.test", "c.test"):
        respx.get(f"https://{host}/robots.txt").mock(httpx.Response(200, text=ALLOW_ALL))
        respx.route(host=host).mock(side_effect=rec.handler)

    # Distinct IPs, so only the domain key is in play.
    dns = StubDNS({"a.test": "198.51.100.1", "b.test": "198.51.100.2", "c.test": "198.51.100.3"})
    f = make_fetcher(client, redis, dns=dns, burst=1)

    results = await asyncio.gather(
        *(f.fetch(CrawlTask(url=f"https://{h}/p")) for h in ("a.test", "b.test", "c.test"))
    )
    assert all(r.outcome is Outcome.FETCHED for r in results)


@respx.mock
async def test_shared_hosting_ip_is_also_rate_limited(client, redis):
    """Why the key is (domain, IP) and not hostname alone.

    One shared-hosting IP can serve tens of thousands of virtual hosts. Limiting
    per hostname lets us open a connection to each of them simultaneously and
    melt one machine, while every individual host limit looks respected.
    """
    rec = IntervalRecorder()
    hosts = [f"site{i}.test" for i in range(12)]
    for host in hosts:
        respx.get(f"https://{host}/robots.txt").mock(httpx.Response(200, text=ALLOW_ALL))
        respx.route(host=host).mock(side_effect=rec.handler)

    # All twelve distinct domains resolve to ONE machine.
    dns = StubDNS({h: "203.0.113.99" for h in hosts})
    f = make_fetcher(client, redis, dns=dns, burst=1)

    results = await asyncio.gather(*(f.fetch(CrawlTask(url=f"https://{h}/p")) for h in hosts))

    admitted = sum(1 for r in results if r.outcome is Outcome.FETCHED)
    blocked_by_ip = [r for r in results if r.outcome is Outcome.RATE_LIMITED]
    # The IP bucket is deliberately more generous than the domain bucket (it is a
    # backstop, not the primary control), but it must not let all twelve through.
    assert admitted < len(hosts), "the shared IP imposed no limit at all"
    assert blocked_by_ip, "no request was held back by the IP key"


@respx.mock
async def test_a_throttled_host_closes_for_every_url_on_it(client, redis):
    """429 is a statement about the server, so the whole host waits."""
    respx.get("https://example.com/robots.txt").mock(httpx.Response(200, text=ALLOW_ALL))
    respx.get("https://example.com/first").mock(
        httpx.Response(429, headers={"Retry-After": "300"})
    )
    others = respx.route(host="example.com").mock(httpx.Response(200, html="x"))

    f = make_fetcher(client, redis, dns=StubDNS(), burst=8, rate=50.0)
    first = await f.fetch(CrawlTask(url="https://example.com/first"))
    assert first.outcome is Outcome.THROTTLED

    before = others.call_count
    rest = await asyncio.gather(
        *(f.fetch(CrawlTask(url=f"https://example.com/x{i}")) for i in range(5))
    )
    assert all(r.outcome is Outcome.RATE_LIMITED for r in rest)
    assert others.call_count == before, "requests leaked through to a paused host"


@respx.mock
async def test_robots_is_fetched_once_not_per_url(client, redis):
    """A robots fetch per URL would itself be an impoliteness."""
    route = respx.get("https://example.com/robots.txt").mock(
        httpx.Response(200, text=ALLOW_ALL)
    )
    respx.route(host="example.com").mock(httpx.Response(200, html="x"))

    f = make_fetcher(client, redis, dns=StubDNS(), burst=32, rate=100.0)
    await asyncio.gather(
        *(f.fetch(CrawlTask(url=f"https://example.com/p{i}")) for i in range(10))
    )
    assert route.call_count == 1
