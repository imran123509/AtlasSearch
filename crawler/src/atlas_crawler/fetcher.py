"""The fetch loop.

    lease URL
      → resolve DNS                  (also gives us half the politeness key)
      → check robots                 (fail closed)
      → check (domain, ip) budget    (atomic, both keys)
      → conditional GET with a hard deadline
      → classify per the response table
      → feed the AIMD controller
      → release lease

Redirects are followed here rather than by httpx, because every hop is a real
request to a possibly different host and needs its own robots check and its own
politeness budget. Letting the HTTP client follow them silently bypasses both.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

import httpx
import structlog

from .config import Config
from .dns import DNSCache
from .models import CrawlTask, FetchResult, HostKey, Outcome, utcnow
from .ratelimit import PolitenessLimiter
from .robots import RobotsCache, header_forbids_indexing
from .urlnorm import canonicalise, is_crawlable_scheme, registrable_domain

log = structlog.get_logger(__name__)

_REDIRECT_CODES = {301, 302, 303, 307, 308}
_PERMANENT_REDIRECTS = {301, 308}


@dataclass(slots=True)
class _Hop:
    url: str
    status: int
    location: str | None = None


@dataclass(slots=True)
class FetchContext:
    """Everything one fetch needs, threaded through so nothing reaches for globals."""

    client: httpx.AsyncClient
    dns: DNSCache
    robots: RobotsCache
    limiter: PolitenessLimiter
    config: Config
    chain: list[_Hop] = field(default_factory=list)


class Fetcher:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        dns: DNSCache,
        robots: RobotsCache,
        limiter: PolitenessLimiter,
        config: Config,
    ) -> None:
        self.client = client
        self.dns = dns
        self.robots = robots
        self.limiter = limiter
        self.cfg = config
        self._sem = asyncio.Semaphore(config.fetch.max_concurrent)

    # -- public -------------------------------------------------------------

    async def fetch(self, task: CrawlTask) -> FetchResult:
        """Fetch one task, following redirects with per-hop politeness."""
        async with self._sem:
            started = time.monotonic()
            try:
                async with asyncio.timeout(self.cfg.fetch.total_timeout):
                    return await self._fetch_chain(task, started)
            except TimeoutError:
                # A tarpitting host must not hold a slot open indefinitely.
                elapsed = time.monotonic() - started
                domain = registrable_domain(task.url)
                await self.limiter.record(domain, signal="err")
                log.info("fetch.timeout", url=task.url, elapsed=round(elapsed, 2))
                return FetchResult(
                    url=task.url,
                    outcome=Outcome.TIMEOUT,
                    elapsed_s=elapsed,
                    error="deadline exceeded",
                )

    # -- internals ----------------------------------------------------------

    async def _fetch_chain(self, task: CrawlTask, started: float) -> FetchResult:
        url = canonicalise(task.url)
        seen: set[str] = set()
        chain: list[_Hop] = []
        first_hop_permanent = False
        deadline = started + self.cfg.fetch.total_timeout

        for hop_index in range(self.cfg.fetch.max_redirects + 1):
            if url in seen:
                return self._terminal(
                    task.url, Outcome.REDIRECT_LOOP, started, error=f"cycle at {url}"
                )
            seen.add(url)

            if not is_crawlable_scheme(url):
                return self._terminal(
                    task.url, Outcome.CLIENT_ERROR, started, error=f"unsupported scheme: {url}"
                )

            # A redirect chain is one logical fetch. Every hop is a real request
            # and must be paid for, but abandoning the chain because hop 2 has no
            # token yet means a multi-hop chain on a slow host can never complete:
            # the retry lands on the same wall. So mid-chain hops wait for their
            # slot, bounded by the deadline we are already under.
            gate = await self._gate(url, wait=hop_index > 0, deadline=deadline)
            if gate is not None:
                return self._terminal(task.url, gate[0], started, retry_after_s=gate[1])

            result = await self._one_hop(url, task if hop_index == 0 else None, started)

            if result.outcome is not Outcome.REDIRECTED:
                # Terminal. Report the final URL and how we got here.
                result.url = task.url
                if url != canonicalise(task.url):
                    result.redirect_target = url
                    result.redirect_permanent = first_hop_permanent
                return result

            chain.append(_Hop(url=url, status=result.status or 0, location=result.redirect_target))
            if hop_index == 0:
                first_hop_permanent = (result.status or 0) in _PERMANENT_REDIRECTS

            target = result.redirect_target
            if not target:
                return self._terminal(
                    task.url, Outcome.CLIENT_ERROR, started, error="redirect without Location"
                )
            url = target

        return self._terminal(
            task.url,
            Outcome.REDIRECT_LOOP,
            started,
            error=f"exceeded {self.cfg.fetch.max_redirects} redirects",
        )

    async def _gate(
        self, url: str, *, wait: bool = False, deadline: float | None = None
    ) -> tuple[Outcome, float] | None:
        """DNS + robots + politeness. Returns a blocking outcome, or None to proceed."""
        host = httpx.URL(url).host
        domain = registrable_domain(url)

        ip = await self.dns.resolve(host)
        if ip is None:
            await self.limiter.record(domain, signal="err")
            return (Outcome.DNS_FAILED, 0.0)

        allowed, rules = await self.robots.can_fetch(url)
        if not allowed:
            outcome = (
                Outcome.ROBOTS_DENIED if rules.reachable else Outcome.ROBOTS_UNREACHABLE
            )
            return (outcome, 0.0)

        # A declared Crawl-delay is a floor on our interval, never a ceiling.
        if (delay := rules.crawl_delay(self.cfg.robots_agent)) is not None:
            await self.limiter.apply_rate_ceiling(domain, 1.0 / max(delay, 0.001))

        host_key = HostKey(domain=domain, ip=ip)
        decision = await self.limiter.acquire(host_key)
        if decision.allowed:
            return None

        # A paused host (429/503) is never worth waiting on inline — the backoff
        # is measured in minutes. Requeue instead.
        if not wait or decision.reason == "paused":
            return (Outcome.RATE_LIMITED, decision.retry_after_s)

        remaining = (deadline - time.monotonic()) if deadline else 0.0
        if decision.retry_after_s + 0.25 >= remaining:
            return (Outcome.RATE_LIMITED, decision.retry_after_s)

        await asyncio.sleep(decision.retry_after_s + 0.01)
        decision = await self.limiter.acquire(host_key)
        if not decision.allowed:
            return (Outcome.RATE_LIMITED, decision.retry_after_s)

        return None

    async def _one_hop(self, url: str, task: CrawlTask | None, started: float) -> FetchResult:
        headers = {
            "User-Agent": self.cfg.user_agent,
            "Accept": self.cfg.fetch.accept,
            "Accept-Language": self.cfg.fetch.accept_language,
            "Accept-Encoding": "gzip, deflate, br",
        }
        # Conditional GET — 60% of refresh fetches end here at ~1 KB.
        if task is not None:
            if task.etag:
                headers["If-None-Match"] = task.etag
            if task.last_modified:
                headers["If-Modified-Since"] = task.last_modified

        domain = registrable_domain(url)

        try:
            async with self.client.stream(
                "GET", url, headers=headers, follow_redirects=False
            ) as resp:
                status = resp.status_code
                rheaders = {k.lower(): v for k, v in resp.headers.items()}

                if status in _REDIRECT_CODES:
                    await resp.aclose()
                    loc = rheaders.get("location")
                    target = canonicalise(loc, base=url) if loc else None
                    await self.limiter.record(domain, signal="ok")
                    return FetchResult(
                        url=url,
                        outcome=Outcome.REDIRECTED,
                        status=status,
                        headers=rheaders,
                        redirect_target=target,
                        elapsed_s=time.monotonic() - started,
                    )

                if status == 304:
                    await resp.aclose()
                    await self.limiter.record(domain, signal="ok")
                    return FetchResult(
                        url=url,
                        outcome=Outcome.NOT_MODIFIED,
                        status=304,
                        headers=rheaders,
                        elapsed_s=time.monotonic() - started,
                    )

                if status in (429, 503):
                    await resp.aclose()
                    retry_after = _parse_retry_after(rheaders.get("retry-after"))
                    await self.limiter.pause_host(
                        domain, retry_after or 60.0, reason=f"http {status}"
                    )
                    return FetchResult(
                        url=url,
                        outcome=Outcome.THROTTLED,
                        status=status,
                        headers=rheaders,
                        retry_after_s=retry_after,
                        elapsed_s=time.monotonic() - started,
                    )

                if status in (404, 410):
                    await resp.aclose()
                    await self.limiter.record(domain, signal="ok")
                    return FetchResult(
                        url=url,
                        outcome=Outcome.GONE,
                        status=status,
                        headers=rheaders,
                        elapsed_s=time.monotonic() - started,
                    )

                if 400 <= status < 500:
                    await resp.aclose()
                    await self.limiter.record(domain, signal="ok")
                    return FetchResult(
                        url=url,
                        outcome=Outcome.CLIENT_ERROR,
                        status=status,
                        headers=rheaders,
                        elapsed_s=time.monotonic() - started,
                    )

                if status >= 500:
                    await resp.aclose()
                    await self.limiter.record(domain, signal="err")
                    return FetchResult(
                        url=url,
                        outcome=Outcome.SERVER_ERROR,
                        status=status,
                        headers=rheaders,
                        elapsed_s=time.monotonic() - started,
                    )

                # 2xx — check the declared size before reading anything.
                declared = _int_or_none(rheaders.get("content-length"))
                if declared is not None and declared > self.cfg.fetch.max_body_bytes:
                    await resp.aclose()
                    await self.limiter.record(domain, signal="ok")
                    return FetchResult(
                        url=url,
                        outcome=Outcome.TOO_LARGE,
                        status=status,
                        headers=rheaders,
                        elapsed_s=time.monotonic() - started,
                        error=f"content-length {declared}",
                    )

                body, truncated = await self._read_capped(resp)
                elapsed = time.monotonic() - started

                if truncated:
                    await self.limiter.record_outcome(domain, ok=True, elapsed_s=elapsed)
                    return FetchResult(
                        url=url,
                        outcome=Outcome.TOO_LARGE,
                        status=status,
                        headers=rheaders,
                        elapsed_s=elapsed,
                        error="body exceeded cap mid-stream",
                    )

                await self.limiter.record_outcome(domain, ok=True, elapsed_s=elapsed)
                return FetchResult(
                    url=url,
                    outcome=Outcome.FETCHED,
                    status=status,
                    headers=rheaders,
                    body=body,
                    content_hash="sha256:" + hashlib.sha256(body).hexdigest(),
                    elapsed_s=elapsed,
                    fetched_at=utcnow(),
                )

        except httpx.HTTPError as exc:
            await self.limiter.record(domain, signal="err")
            log.info("fetch.network_error", url=url, error=type(exc).__name__)
            return FetchResult(
                url=url,
                outcome=Outcome.NETWORK_ERROR,
                elapsed_s=time.monotonic() - started,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _read_capped(self, resp: httpx.Response) -> tuple[bytes, bool]:
        """Read the body, stopping at the cap rather than trusting Content-Length.

        A server may under-declare, or not declare at all. Streaming and counting
        is the only defence against a decompression bomb or an endless response.
        """
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > self.cfg.fetch.max_body_bytes:
                return (b"".join(chunks), True)
            chunks.append(chunk)
        return (b"".join(chunks), False)

    def _terminal(
        self,
        url: str,
        outcome: Outcome,
        started: float,
        *,
        retry_after_s: float | None = None,
        error: str | None = None,
    ) -> FetchResult:
        return FetchResult(
            url=url,
            outcome=outcome,
            elapsed_s=time.monotonic() - started,
            retry_after_s=retry_after_s,
            error=error,
        )

    def is_indexable(self, result: FetchResult) -> bool:
        """`X-Robots-Tag: noindex` means we may crawl it but must not index it."""
        return not header_forbids_indexing(result.headers, self.cfg.robots_agent)


def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After is either delta-seconds or an HTTP-date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, (when - utcnow()).total_seconds())


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
