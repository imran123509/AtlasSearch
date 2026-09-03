"""Crawler worker: wiring, the outcome handler, and graceful shutdown.

Shutdown matters more than it looks. The Kubernetes StatefulSet gives this
process 90 seconds to finish in-flight fetches and release its leases; killing it
sooner leaves leases outstanding and creates a dual-ownership window in which two
workers can hit the same host. See features/KUBERNETES.md.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys

import httpx
import structlog

from .config import Config
from .dns import DNSCache
from .emit import Emitter, KafkaEmitter, NullEmitter, doc_id, fetched_event, should_emit
from .fetcher import Fetcher
from .frontier import Frontier
from .metrics import (
    BYTES_FETCHED,
    DNS_CACHE,
    FETCH_DURATION,
    FETCHES,
    HOSTS_DUE,
    HOSTS_SCHEDULED,
    IDLE_FETCHERS,
    ROBOTS_DENIED,
    status_class,
)
from .models import CrawlTask, FetchResult, Outcome
from .ratelimit import PolitenessLimiter
from .robots import RobotsCache
from .storage import BlobStore, LocalBlobStore, S3BlobStore
from .urlnorm import registrable_domain

log = structlog.get_logger(__name__)


class CrawlWorker:
    def __init__(
        self,
        *,
        config: Config,
        frontier: Frontier,
        fetcher: Fetcher,
        blobs: BlobStore,
        emitter: Emitter,
    ) -> None:
        self.cfg = config
        self.frontier = frontier
        self.fetcher = fetcher
        self.blobs = blobs
        self.emitter = emitter
        self._stopping = asyncio.Event()
        self._inflight: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        log.info("crawler.start", user_agent=self.cfg.user_agent)
        idle_rounds = 0
        while not self._stopping.is_set():
            capacity = self.cfg.fetch.max_concurrent - len(self._inflight)
            if capacity <= 0:
                await asyncio.sleep(0.05)
                continue

            tasks = await self.frontier.lease(capacity)
            if not tasks:
                idle_rounds += 1
                IDLE_FETCHERS.set(capacity)
                # Nothing due. Sleep briefly rather than spinning on Redis.
                await asyncio.sleep(min(0.5 * idle_rounds, 5.0))
                continue

            idle_rounds = 0
            IDLE_FETCHERS.set(0)
            for task in tasks:
                t = asyncio.create_task(self._process(task))
                self._inflight.add(t)
                t.add_done_callback(self._inflight.discard)

            await self._publish_stats()

        await self._drain()

    async def _process(self, task: CrawlTask) -> None:
        try:
            result = await self.fetcher.fetch(task)
            FETCH_DURATION.observe(result.elapsed_s)
            FETCHES.labels(str(result.outcome), status_class(result.status)).inc()
            await self._handle(task, result)
        except asyncio.CancelledError:
            # Shutdown. Leave the lease alone — TTL expiry returns the URL.
            raise
        except Exception:  # noqa: BLE001 - one bad page must not kill the worker
            log.exception("crawler.process_failed", url=task.url)
            await self.frontier.release(task)

    async def _handle(self, task: CrawlTask, result: FetchResult) -> None:
        """The response-handling table from features/WEB-CRAWLER.md."""
        outcome = result.outcome
        canonical = result.redirect_target or result.url

        match outcome:
            case Outcome.FETCHED:
                BYTES_FETCHED.inc(len(result.body or b""))
                blob = await self.blobs.put(result)
                await self.frontier.store_validators(
                    task.url, etag=result.etag, last_modified=result.last_modified
                )
                if self.fetcher.is_indexable(result):
                    await self.emitter.emit(
                        self.cfg.topic_fetched,
                        doc_id(canonical),
                        fetched_event(result, blob, canonical_url=canonical),
                    )
                else:
                    # X-Robots-Tag: noindex — we may store it, we may not index it.
                    log.info("crawler.noindex", url=canonical)
                await self.frontier.release(task)

            case Outcome.NOT_MODIFIED:
                # Cheapest possible outcome. Touch metadata, tell the scheduler
                # this page did not change so its λ estimate can fall.
                await self.emitter.emit(
                    self.cfg.topic_fetched, doc_id(canonical), fetched_event(result, None)
                )
                await self.frontier.release(task)

            case Outcome.GONE:
                await self.emitter.emit(
                    self.cfg.topic_fetched, doc_id(canonical), fetched_event(result, None)
                )
                await self.frontier.release(task)

            case Outcome.THROTTLED:
                # The host has already been paused wholesale by the limiter.
                # Requeue this URL past the pause window.
                await self.frontier.requeue(task, delay_s=result.retry_after_s or 60.0)

            case Outcome.SERVER_ERROR:
                task.attempt += 1
                if task.attempt >= self.cfg.fetch.max_server_error_retries:
                    log.info("crawler.giving_up", url=task.url, attempts=task.attempt)
                    await self.frontier.release(task)
                else:
                    await self.frontier.requeue(task, delay_s=30.0 * (2**task.attempt))

            case Outcome.RATE_LIMITED:
                # Our own limiter, not the host's. No penalty, just try later.
                await self.frontier.requeue(task, delay_s=max(result.retry_after_s or 1.0, 1.0))

            case Outcome.ROBOTS_DENIED:
                ROBOTS_DENIED.labels("disallowed").inc()
                await self.frontier.release(task)

            case Outcome.ROBOTS_UNREACHABLE:
                # Fail closed, and retry later — the site may be having a bad day.
                ROBOTS_DENIED.labels("unreachable").inc()
                await self.frontier.requeue(task, delay_s=self.cfg.robots.negative_ttl_seconds)

            case Outcome.TIMEOUT | Outcome.NETWORK_ERROR | Outcome.DNS_FAILED:
                task.attempt += 1
                if task.attempt >= self.cfg.fetch.max_server_error_retries:
                    await self.frontier.release(task)
                else:
                    await self.frontier.requeue(task, delay_s=60.0 * task.attempt)

            case _:
                # CLIENT_ERROR, TOO_LARGE, REDIRECT_LOOP: terminal, no retry.
                await self.frontier.release(task)

    async def _publish_stats(self) -> None:
        stats = await self.frontier.stats()
        HOSTS_SCHEDULED.set(stats["hosts_scheduled"])
        HOSTS_DUE.set(stats["hosts_due"])
        d = self.fetcher.dns.stats()
        DNS_CACHE.labels("total").set(d["entries"])
        DNS_CACHE.labels("live").set(d["live"])

    async def _drain(self) -> None:
        if not self._inflight:
            return
        log.info("crawler.draining", inflight=len(self._inflight))
        done, pending = await asyncio.wait(self._inflight, timeout=60.0)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        log.info("crawler.drained", completed=len(done), cancelled=len(pending))

    def stop(self) -> None:
        log.info("crawler.stop_requested")
        self._stopping.set()


async def build_worker(cfg: Config, *, dry_run: bool = False, blob_dir: str | None = None):
    import redis.asyncio as aioredis

    redis = aioredis.from_url(cfg.redis_url, decode_responses=False)

    limits = httpx.Limits(
        max_connections=cfg.fetch.max_concurrent * 2,
        max_keepalive_connections=cfg.fetch.max_concurrent,
    )
    timeout = httpx.Timeout(
        cfg.fetch.total_timeout,
        connect=cfg.fetch.connect_timeout,
        read=cfg.fetch.read_timeout,
    )
    client = httpx.AsyncClient(limits=limits, timeout=timeout, http2=True)

    dns = DNSCache(cfg.dns)
    robots = RobotsCache(
        client, redis,
        user_agent=cfg.user_agent,
        robots_agent=cfg.robots_agent,
        config=cfg.robots,
    )
    limiter = PolitenessLimiter(redis, cfg.politeness)
    fetcher = Fetcher(client=client, dns=dns, robots=robots, limiter=limiter, config=cfg)
    frontier = Frontier(redis, cfg)

    blobs: BlobStore = (
        LocalBlobStore(blob_dir or "./blobs")
        if dry_run or blob_dir
        else S3BlobStore(endpoint=cfg.s3_endpoint, bucket=cfg.s3_bucket)
    )
    emitter: Emitter = NullEmitter() if dry_run else KafkaEmitter(cfg.kafka_brokers)
    await emitter.start()

    worker = CrawlWorker(
        config=cfg, frontier=frontier, fetcher=fetcher, blobs=blobs, emitter=emitter
    )
    return worker, client, emitter, blobs, redis


async def amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-crawler")
    parser.add_argument("--seed", action="append", default=[], help="Seed URL (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="Local blobs, no Kafka")
    parser.add_argument("--blob-dir", default=None)
    parser.add_argument("--metrics-port", type=int, default=0)
    args = parser.parse_args(argv)

    structlog.configure(processors=[structlog.processors.add_log_level,
                                    structlog.processors.TimeStamper(fmt="iso"),
                                    structlog.dev.ConsoleRenderer()])

    cfg = Config()
    cfg.validate()  # refuses a user-agent with no contact URL

    if args.metrics_port:
        from prometheus_client import start_http_server

        start_http_server(args.metrics_port)

    worker, client, emitter, blobs, redis = await build_worker(
        cfg, dry_run=args.dry_run, blob_dir=args.blob_dir
    )

    for url in args.seed:
        added = await worker.frontier.add(url, priority=0)
        log.info("crawler.seeded", url=url, accepted=added)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows lacks SIGTERM handlers
            loop.add_signal_handler(sig, worker.stop)

    try:
        await worker.run()
    finally:
        await client.aclose()
        await emitter.stop()
        await blobs.close()
        await redis.aclose()
    return 0


def run() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    run()
