from __future__ import annotations

import httpx
import pytest
import respx

from atlas_crawler.fetcher import Fetcher, _parse_retry_after
from atlas_crawler.models import CrawlTask, Outcome
from atlas_crawler.ratelimit import PolitenessLimiter
from atlas_crawler.robots import RobotsCache

ALLOW_ALL = "User-agent: *\nAllow: /\n"


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


@pytest.fixture
def fetcher(client, redis, cfg, dns):
    robots = RobotsCache(
        client, redis,
        user_agent=cfg.user_agent, robots_agent=cfg.robots_agent, config=cfg.robots,
    )
    limiter = PolitenessLimiter(redis, cfg.politeness)
    return Fetcher(client=client, dns=dns, robots=robots, limiter=limiter, config=cfg)


def _allow_robots(host: str = "example.com") -> None:
    respx.get(f"https://{host}/robots.txt").mock(httpx.Response(200, text=ALLOW_ALL))


class TestResponseTable:
    @respx.mock
    async def test_200_is_fetched_and_hashed(self, fetcher):
        _allow_robots()
        respx.get("https://example.com/p").mock(
            httpx.Response(200, html="<html>hi</html>", headers={"ETag": 'W/"abc"'})
        )
        r = await fetcher.fetch(CrawlTask(url="https://example.com/p"))
        assert r.outcome is Outcome.FETCHED
        assert r.body == b"<html>hi</html>"
        assert r.content_hash and r.content_hash.startswith("sha256:")
        assert r.etag == 'W/"abc"'

    @respx.mock
    async def test_304_sends_validators_and_transfers_no_body(self, fetcher):
        _allow_robots()
        route = respx.get("https://example.com/p").mock(httpx.Response(304))
        r = await fetcher.fetch(
            CrawlTask(url="https://example.com/p", etag='W/"abc"', last_modified="Mon, 1 Sep 2025 00:00:00 GMT")
        )
        assert r.outcome is Outcome.NOT_MODIFIED
        assert r.body is None
        sent = route.calls.last.request.headers
        assert sent["if-none-match"] == 'W/"abc"'
        assert sent["if-modified-since"] == "Mon, 1 Sep 2025 00:00:00 GMT"

    @pytest.mark.parametrize("status", [404, 410])
    @respx.mock
    async def test_404_and_410_are_gone(self, fetcher, status):
        _allow_robots()
        respx.get("https://example.com/p").mock(httpx.Response(status))
        r = await fetcher.fetch(CrawlTask(url="https://example.com/p"))
        assert r.outcome is Outcome.GONE

    @respx.mock
    async def test_400_is_a_terminal_client_error(self, fetcher):
        _allow_robots()
        respx.get("https://example.com/p").mock(httpx.Response(400))
        r = await fetcher.fetch(CrawlTask(url="https://example.com/p"))
        assert r.outcome is Outcome.CLIENT_ERROR

    @respx.mock
    async def test_500_is_a_retryable_server_error(self, fetcher):
        _allow_robots()
        respx.get("https://example.com/p").mock(httpx.Response(500))
        r = await fetcher.fetch(CrawlTask(url="https://example.com/p"))
        assert r.outcome is Outcome.SERVER_ERROR

    @pytest.mark.parametrize("status", [429, 503])
    @respx.mock
    async def test_429_and_503_pause_the_whole_host(self, fetcher, status):
        """Back off the host, not just this URL — it is a statement about the server."""
        _allow_robots()
        respx.get("https://example.com/a").mock(
            httpx.Response(status, headers={"Retry-After": "120"})
        )
        r = await fetcher.fetch(CrawlTask(url="https://example.com/a"))
        assert r.outcome is Outcome.THROTTLED
        assert r.retry_after_s == 120.0
        assert await fetcher.limiter.paused_for("example.com") > 0

        # A different URL on the same host is now closed too.
        respx.get("https://example.com/b").mock(httpx.Response(200, html="x"))
        r2 = await fetcher.fetch(CrawlTask(url="https://example.com/b"))
        assert r2.outcome is Outcome.RATE_LIMITED


class TestRedirects:
    @respx.mock
    async def test_chain_is_followed_to_a_terminal_response(self, fetcher):
        _allow_robots()
        respx.get("https://example.com/a").mock(
            httpx.Response(302, headers={"Location": "/b"})
        )
        respx.get("https://example.com/b").mock(httpx.Response(200, html="done"))
        r = await fetcher.fetch(CrawlTask(url="https://example.com/a"))
        assert r.outcome is Outcome.FETCHED
        assert r.body == b"done"
        assert r.redirect_target == "https://example.com/b"

    @respx.mock
    async def test_301_is_marked_permanent(self, fetcher):
        _allow_robots()
        respx.get("https://example.com/a").mock(
            httpx.Response(301, headers={"Location": "/b"})
        )
        respx.get("https://example.com/b").mock(httpx.Response(200, html="x"))
        r = await fetcher.fetch(CrawlTask(url="https://example.com/a"))
        assert r.redirect_permanent is True

    @respx.mock
    async def test_302_is_not_permanent(self, fetcher):
        _allow_robots()
        respx.get("https://example.com/a").mock(
            httpx.Response(302, headers={"Location": "/b"})
        )
        respx.get("https://example.com/b").mock(httpx.Response(200, html="x"))
        r = await fetcher.fetch(CrawlTask(url="https://example.com/a"))
        assert r.redirect_permanent is False

    @respx.mock
    async def test_cycle_is_detected(self, fetcher):
        _allow_robots()
        respx.get("https://example.com/a").mock(httpx.Response(302, headers={"Location": "/b"}))
        respx.get("https://example.com/b").mock(httpx.Response(302, headers={"Location": "/a"}))
        r = await fetcher.fetch(CrawlTask(url="https://example.com/a"))
        assert r.outcome is Outcome.REDIRECT_LOOP

    @respx.mock
    async def test_depth_is_capped(self, fetcher, cfg):
        _allow_robots()
        for i in range(20):
            respx.get(f"https://example.com/h{i}").mock(
                httpx.Response(302, headers={"Location": f"/h{i + 1}"})
            )
        r = await fetcher.fetch(CrawlTask(url="https://example.com/h0"))
        assert r.outcome is Outcome.REDIRECT_LOOP

    @respx.mock
    async def test_cross_host_hop_checks_the_new_hosts_robots(self, fetcher):
        """A redirect off-site must not inherit the origin's permission."""
        _allow_robots("example.com")
        respx.get("https://example.com/a").mock(
            httpx.Response(302, headers={"Location": "https://other.test/x"})
        )
        respx.get("https://other.test/robots.txt").mock(
            httpx.Response(200, text="User-agent: *\nDisallow: /")
        )
        target = respx.get("https://other.test/x").mock(httpx.Response(200, html="secret"))
        r = await fetcher.fetch(CrawlTask(url="https://example.com/a"))
        assert r.outcome is Outcome.ROBOTS_DENIED
        assert target.call_count == 0


class TestGuards:
    @respx.mock
    async def test_robots_denial_short_circuits_before_any_request(self, fetcher):
        respx.get("https://example.com/robots.txt").mock(
            httpx.Response(200, text="User-agent: *\nDisallow: /")
        )
        page = respx.get("https://example.com/p").mock(httpx.Response(200, html="x"))
        r = await fetcher.fetch(CrawlTask(url="https://example.com/p"))
        assert r.outcome is Outcome.ROBOTS_DENIED
        assert page.call_count == 0

    @respx.mock
    async def test_unreachable_robots_fails_closed(self, fetcher):
        respx.get("https://example.com/robots.txt").mock(httpx.Response(503))
        page = respx.get("https://example.com/p").mock(httpx.Response(200, html="x"))
        r = await fetcher.fetch(CrawlTask(url="https://example.com/p"))
        assert r.outcome is Outcome.ROBOTS_UNREACHABLE
        assert page.call_count == 0

    @respx.mock
    async def test_declared_oversize_body_is_not_downloaded(self, fetcher, cfg):
        _allow_robots()
        route = respx.get("https://example.com/big").mock(
            httpx.Response(200, headers={"Content-Length": str(cfg.fetch.max_body_bytes * 10)},
                           content=b"x" * 16)
        )
        r = await fetcher.fetch(CrawlTask(url="https://example.com/big"))
        assert r.outcome is Outcome.TOO_LARGE
        assert route.called

    @respx.mock
    async def test_undeclared_oversize_body_is_capped_mid_stream(self, fetcher, cfg):
        """Content-Length may be absent or lying; streaming and counting is the defence."""
        _allow_robots()
        respx.get("https://example.com/bomb").mock(
            httpx.Response(200, content=b"x" * (cfg.fetch.max_body_bytes * 3))
        )
        r = await fetcher.fetch(CrawlTask(url="https://example.com/bomb"))
        assert r.outcome is Outcome.TOO_LARGE

    @respx.mock
    async def test_dns_failure_is_terminal(self, client, redis, cfg):
        from conftest import StubDNS

        robots = RobotsCache(client, redis, user_agent=cfg.user_agent,
                             robots_agent=cfg.robots_agent, config=cfg.robots)
        f = Fetcher(client=client, dns=StubDNS(default=""), robots=robots,
                    limiter=PolitenessLimiter(redis, cfg.politeness), config=cfg)
        r = await f.fetch(CrawlTask(url="https://nxdomain.test/p"))
        assert r.outcome is Outcome.DNS_FAILED

    @respx.mock
    async def test_network_error_is_recorded_not_raised(self, fetcher):
        _allow_robots()
        respx.get("https://example.com/p").mock(side_effect=httpx.ConnectError("refused"))
        r = await fetcher.fetch(CrawlTask(url="https://example.com/p"))
        assert r.outcome is Outcome.NETWORK_ERROR
        assert r.error

    @respx.mock
    async def test_non_http_scheme_rejected(self, fetcher):
        r = await fetcher.fetch(CrawlTask(url="ftp://example.com/f"))
        assert r.outcome is Outcome.CLIENT_ERROR

    @respx.mock
    async def test_user_agent_is_sent(self, fetcher, cfg):
        _allow_robots()
        route = respx.get("https://example.com/p").mock(httpx.Response(200, html="x"))
        await fetcher.fetch(CrawlTask(url="https://example.com/p"))
        assert route.calls.last.request.headers["user-agent"] == cfg.user_agent

    @respx.mock
    async def test_noindex_header_marks_result_unindexable(self, fetcher):
        _allow_robots()
        respx.get("https://example.com/p").mock(
            httpx.Response(200, html="x", headers={"X-Robots-Tag": "noindex"})
        )
        r = await fetcher.fetch(CrawlTask(url="https://example.com/p"))
        assert r.outcome is Outcome.FETCHED  # crawling was permitted
        assert fetcher.is_indexable(r) is False  # indexing is not


class TestRetryAfterParsing:
    def test_delta_seconds(self):
        assert _parse_retry_after("120") == 120.0

    def test_http_date(self):
        v = _parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
        assert v is not None and v > 0

    def test_past_date_clamps_to_zero(self):
        assert _parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0

    def test_garbage(self):
        assert _parse_retry_after("soon") is None

    def test_absent(self):
        assert _parse_retry_after(None) is None
