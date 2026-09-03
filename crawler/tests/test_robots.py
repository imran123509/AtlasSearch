from __future__ import annotations

import httpx
import pytest
import respx

from atlas_crawler.robots import RobotsCache, header_forbids_indexing, origin_of

ROBOTS = """
User-agent: *
Disallow: /private/
Crawl-delay: 5

User-agent: atlassearchtestbot
Disallow: /nope/
"""


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


@pytest.fixture
def cache(client, redis, cfg):
    return RobotsCache(
        client, redis,
        user_agent=cfg.user_agent,
        robots_agent=cfg.robots_agent,
        config=cfg.robots,
    )


def test_origin_is_scheme_host_port_not_registrable_domain():
    assert origin_of("https://a.example.com/x/y?q=1") == "https://a.example.com"
    assert origin_of("http://example.com:8080/x") == "http://example.com:8080"


@respx.mock
async def test_allows_and_denies_per_rules(cache):
    respx.get("https://example.com/robots.txt").mock(httpx.Response(200, text=ROBOTS))
    assert (await cache.can_fetch("https://example.com/public/a"))[0] is True
    assert (await cache.can_fetch("https://example.com/nope/a"))[0] is False


@respx.mock
async def test_404_means_no_rules_exist_so_allow(cache):
    """RFC 9309: 4xx 'unavailable' — no rules, everything permitted."""
    respx.get("https://example.com/robots.txt").mock(httpx.Response(404))
    allowed, rules = await cache.can_fetch("https://example.com/anything")
    assert allowed is True
    assert rules.reachable is True


@pytest.mark.parametrize("status", [500, 502, 503, 429])
@respx.mock
async def test_fails_closed_on_unreachable(cache, status):
    """The single most common correctness bug in hobby crawlers."""
    respx.get("https://example.com/robots.txt").mock(httpx.Response(status))
    allowed, rules = await cache.can_fetch("https://example.com/anything")
    assert allowed is False
    assert rules.reachable is False


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
async def test_fails_closed_on_access_controlled_robots(cache, status):
    """Deliberate deviation from RFC 9309, documented in robots.py."""
    respx.get("https://example.com/robots.txt").mock(httpx.Response(status))
    assert (await cache.can_fetch("https://example.com/x"))[0] is False


@respx.mock
async def test_fails_closed_on_network_error(cache):
    respx.get("https://example.com/robots.txt").mock(side_effect=httpx.ConnectError("nope"))
    assert (await cache.can_fetch("https://example.com/x"))[0] is False


@respx.mock
async def test_fails_closed_on_timeout(cache):
    respx.get("https://example.com/robots.txt").mock(side_effect=httpx.ReadTimeout("slow"))
    assert (await cache.can_fetch("https://example.com/x"))[0] is False


@respx.mock
async def test_wildcard_crawl_delay_applies_even_with_a_specific_group(cache, cfg):
    """Strict robots semantics would ignore this delay; we honour it anyway.

    ROBOTS declares `Crawl-delay: 5` under `*` and a separate group naming our
    bot with no delay. Reading the spec strictly gives us no delay at all — the
    aggressive interpretation of an operator plainly asking for slower crawling.
    """
    respx.get("https://example.com/robots.txt").mock(httpx.Response(200, text=ROBOTS))
    rules = await cache.get("https://example.com/x")
    assert rules.crawl_delay(cfg.robots_agent) == 5.0


@respx.mock
async def test_specific_crawl_delay_wins_when_stricter(cache, cfg):
    respx.get("https://example.com/robots.txt").mock(
        httpx.Response(
            200,
            text="User-agent: *\nCrawl-delay: 2\n\n"
                 f"User-agent: {cfg.robots_agent}\nCrawl-delay: 30\n",
        )
    )
    rules = await cache.get("https://example.com/x")
    assert rules.crawl_delay(cfg.robots_agent) == 30.0


@respx.mock
async def test_no_crawl_delay_declared(cache, cfg):
    respx.get("https://example.com/robots.txt").mock(
        httpx.Response(200, text="User-agent: *\nDisallow: /x\n")
    )
    rules = await cache.get("https://example.com/y")
    assert rules.crawl_delay(cfg.robots_agent) is None


@respx.mock
async def test_result_is_cached_not_refetched(cache):
    route = respx.get("https://example.com/robots.txt").mock(httpx.Response(200, text=ROBOTS))
    for _ in range(5):
        await cache.can_fetch("https://example.com/public/a")
    assert route.call_count == 1


@respx.mock
async def test_unreachable_result_is_also_cached(cache):
    """Otherwise a down site gets hammered with robots.txt requests."""
    route = respx.get("https://example.com/robots.txt").mock(httpx.Response(503))
    for _ in range(5):
        await cache.can_fetch("https://example.com/x")
    assert route.call_count == 1


@respx.mock
async def test_malformed_robots_does_not_crash(cache):
    respx.get("https://example.com/robots.txt").mock(
        httpx.Response(200, content=b"\xff\xfe not really\x00 robots \x01")
    )
    allowed, _ = await cache.can_fetch("https://example.com/x")
    assert isinstance(allowed, bool)


@respx.mock
async def test_per_origin_not_per_domain(cache):
    """robots.txt is scoped to the origin; two subdomains have separate files."""
    respx.get("https://a.example.com/robots.txt").mock(
        httpx.Response(200, text="User-agent: *\nDisallow: /")
    )
    respx.get("https://b.example.com/robots.txt").mock(httpx.Response(200, text=""))
    assert (await cache.can_fetch("https://a.example.com/x"))[0] is False
    assert (await cache.can_fetch("https://b.example.com/x"))[0] is True


class TestXRobotsTag:
    def test_plain_noindex(self):
        assert header_forbids_indexing({"x-robots-tag": "noindex"}, "atlasbot")

    def test_none_is_noindex_plus_nofollow(self):
        assert header_forbids_indexing({"x-robots-tag": "none"}, "atlasbot")

    def test_agent_scoped_match(self):
        assert header_forbids_indexing({"x-robots-tag": "atlasbot: noindex"}, "atlasbot")

    def test_agent_scoped_for_someone_else_is_ignored(self):
        assert not header_forbids_indexing({"x-robots-tag": "googlebot: noindex"}, "atlasbot")

    def test_unrelated_directives_pass(self):
        assert not header_forbids_indexing({"x-robots-tag": "noarchive, nosnippet"}, "atlasbot")

    def test_absent_header(self):
        assert not header_forbids_indexing({}, "atlasbot")
