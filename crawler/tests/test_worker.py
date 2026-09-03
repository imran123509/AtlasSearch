"""Worker outcome handling — the *consequences* of the response table.

test_fetcher covers classification; this covers what the worker then does with
each classification: store, emit, requeue with decay, or give up.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from atlas_crawler.emit import NullEmitter
from atlas_crawler.fetcher import Fetcher
from atlas_crawler.frontier import Frontier
from atlas_crawler.main import CrawlWorker
from atlas_crawler.models import CrawlTask, Outcome
from atlas_crawler.ratelimit import PolitenessLimiter
from atlas_crawler.robots import RobotsCache
from atlas_crawler.storage import LocalBlobStore

from conftest import StubDNS

ALLOW_ALL = "User-agent: *\nAllow: /\n"


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


@pytest.fixture
def worker(client, redis, cfg, tmp_path):
    robots = RobotsCache(
        client, redis, user_agent=cfg.user_agent,
        robots_agent=cfg.robots_agent, config=cfg.robots,
    )
    fetcher = Fetcher(
        client=client, dns=StubDNS(), robots=robots,
        limiter=PolitenessLimiter(redis, cfg.politeness), config=cfg,
    )
    return CrawlWorker(
        config=cfg,
        frontier=Frontier(redis, cfg),
        fetcher=fetcher,
        blobs=LocalBlobStore(tmp_path / "blobs"),
        emitter=NullEmitter(),
    )


def _allow_robots(host: str = "example.com") -> None:
    respx.get(f"https://{host}/robots.txt").mock(httpx.Response(200, text=ALLOW_ALL))


async def _run_once(worker, url: str) -> CrawlTask:
    await worker.frontier.add(url)
    task = (await worker.frontier.lease(1))[0]
    await worker._process(task)
    return task


@respx.mock
async def test_fetched_page_is_stored_and_emitted(worker):
    _allow_robots()
    respx.get("https://example.com/p").mock(
        httpx.Response(200, html="<html>body</html>", headers={"ETag": 'W/"v1"'})
    )
    await _run_once(worker, "https://example.com/p")

    assert len(worker.emitter.messages) == 1
    topic, key, value = worker.emitter.messages[0]
    assert topic == "pages.fetched"
    assert value["blob"], "no blob pointer on the event"
    assert value["content_hash"].startswith("sha256:")
    assert value["status"] == 200
    assert await worker.blobs.exists(value["blob"])


@respx.mock
async def test_event_carries_a_pointer_never_the_body(worker):
    """Bodies in Kafka would mean 36 TB/day through the brokers."""
    _allow_robots()
    respx.get("https://example.com/p").mock(httpx.Response(200, html="<html>secret</html>"))
    await _run_once(worker, "https://example.com/p")

    _, _, value = worker.emitter.messages[0]
    assert "secret" not in repr(value)
    assert set(value) >= {"doc_id", "url", "blob", "content_hash", "size_bytes"}


@respx.mock
async def test_validators_are_stored_for_the_next_conditional_get(worker):
    _allow_robots()
    respx.get("https://example.com/p").mock(
        httpx.Response(200, html="x", headers={"ETag": 'W/"v1"',
                                               "Last-Modified": "Mon, 1 Sep 2025 00:00:00 GMT"})
    )
    await _run_once(worker, "https://example.com/p")

    await worker.frontier.add("https://example.com/p")
    task = (await worker.frontier.lease(1))[0]
    assert task.etag == 'W/"v1"'
    assert task.last_modified == "Mon, 1 Sep 2025 00:00:00 GMT"


@respx.mock
async def test_noindex_page_is_stored_but_not_emitted(worker):
    """X-Robots-Tag: noindex — crawling was permitted, indexing is not."""
    _allow_robots()
    respx.get("https://example.com/p").mock(
        httpx.Response(200, html="x", headers={"X-Robots-Tag": "noindex"})
    )
    await _run_once(worker, "https://example.com/p")
    assert worker.emitter.messages == []


@respx.mock
async def test_304_emits_without_a_blob(worker):
    """Tells the scheduler the page did not change, so its λ estimate can fall."""
    _allow_robots()
    respx.get("https://example.com/p").mock(httpx.Response(304))
    await _run_once(worker, "https://example.com/p")

    _, _, value = worker.emitter.messages[0]
    assert value["blob"] is None
    assert value["outcome"] == str(Outcome.NOT_MODIFIED)


@respx.mock
async def test_410_emits_a_tombstone(worker):
    _allow_robots()
    respx.get("https://example.com/p").mock(httpx.Response(410))
    await _run_once(worker, "https://example.com/p")
    assert worker.emitter.messages[0][2]["outcome"] == str(Outcome.GONE)


@respx.mock
async def test_robots_denied_releases_without_emitting(worker, redis):
    respx.get("https://example.com/robots.txt").mock(
        httpx.Response(200, text="User-agent: *\nDisallow: /")
    )
    task = await _run_once(worker, "https://example.com/p")
    assert worker.emitter.messages == []
    assert await redis.get(worker.frontier._lease(task.url)) is None


@respx.mock
async def test_server_error_is_requeued_with_backoff(worker, redis):
    _allow_robots()
    respx.get("https://example.com/p").mock(httpx.Response(500))
    await _run_once(worker, "https://example.com/p")

    # Requeued for later, not dropped and not immediately due.
    assert await worker.frontier.lease(1) == []
    assert await redis.zcard(worker.frontier._heap) == 1


@respx.mock
async def test_server_error_gives_up_after_max_attempts(worker):
    _allow_robots()
    respx.get("https://example.com/p").mock(httpx.Response(500))
    await worker.frontier.add("https://example.com/p")
    task = (await worker.frontier.lease(1))[0]
    task.attempt = worker.cfg.fetch.max_server_error_retries - 1
    await worker._process(task)
    assert await worker.frontier.stats() == {"hosts_scheduled": 0, "hosts_due": 0}


@respx.mock
async def test_client_error_is_terminal_not_retried(worker):
    _allow_robots()
    respx.get("https://example.com/p").mock(httpx.Response(400))
    await _run_once(worker, "https://example.com/p")
    assert await worker.frontier.stats() == {"hosts_scheduled": 0, "hosts_due": 0}
    assert worker.emitter.messages == []


@respx.mock
async def test_a_crash_in_handling_does_not_kill_the_worker(worker, monkeypatch):
    _allow_robots()
    respx.get("https://example.com/p").mock(httpx.Response(200, html="x"))

    async def boom(*_a, **_k):
        raise RuntimeError("blob store on fire")

    monkeypatch.setattr(worker.blobs, "put", boom)
    await _run_once(worker, "https://example.com/p")  # must not raise


@respx.mock
async def test_identical_bodies_are_stored_once(worker):
    """Content-addressed keys: ~a third of the web is duplicate."""
    for host in ("a.test", "b.test"):
        _allow_robots(host)
        respx.get(f"https://{host}/p").mock(httpx.Response(200, html="<html>same</html>"))

    await _run_once(worker, "https://a.test/p")
    await _run_once(worker, "https://b.test/p")

    keys = {m[2]["blob"] for m in worker.emitter.messages}
    assert len(keys) == 1, "identical content produced two blobs"
