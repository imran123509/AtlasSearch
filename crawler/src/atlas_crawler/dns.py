"""DNS resolution with caching.

At 10K fetches/s an uncached resolver is a second full-scale distributed system
you did not plan for, and public resolvers will rate-limit you. The Target design
runs its own recursive resolvers; the Build uses the system resolver behind this
cache, which is adequate at Build volume.

We need the resolved IP for more than connecting: it is half the politeness key.
"""

from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import dataclass

import structlog

from .config import DNSConfig

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class _Entry:
    ips: tuple[str, ...]
    expires_at: float


class DNSCache:
    def __init__(self, config: DNSConfig | None = None) -> None:
        self.cfg = config or DNSConfig()
        self._cache: dict[str, _Entry] = {}
        # Collapses concurrent lookups for the same host into one query.
        self._inflight: dict[str, asyncio.Future[tuple[str, ...]]] = {}

    async def resolve(self, host: str) -> str | None:
        """Return one IP for `host`, or None if resolution failed.

        Negative results are cached too, briefly — a host that does not resolve
        will not resolve for the next few hundred URLs from that host either.
        """
        ips = await self.resolve_all(host)
        return ips[0] if ips else None

    async def resolve_all(self, host: str) -> tuple[str, ...]:
        now = time.monotonic()
        entry = self._cache.get(host)
        if entry is not None and entry.expires_at > now:
            return entry.ips

        if (existing := self._inflight.get(host)) is not None:
            return await asyncio.shield(existing)

        fut: asyncio.Future[tuple[str, ...]] = asyncio.get_running_loop().create_future()
        self._inflight[host] = fut
        try:
            ips = await self._lookup(host)
            ttl = self.cfg.ttl_seconds if ips else self.cfg.negative_ttl_seconds
            ttl = max(ttl, self.cfg.ttl_floor_seconds if ips else 0)
            self._cache[host] = _Entry(ips=ips, expires_at=now + ttl)
            if not fut.done():
                fut.set_result(ips)
            return ips
        except Exception as exc:  # noqa: BLE001 - resolution failure is data, not a crash
            if not fut.done():
                fut.set_result(())
            self._cache[host] = _Entry(ips=(), expires_at=now + self.cfg.negative_ttl_seconds)
            log.debug("dns.failed", host=host, error=str(exc))
            return ()
        finally:
            self._inflight.pop(host, None)

    async def _lookup(self, host: str) -> tuple[str, ...]:
        loop = asyncio.get_running_loop()
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, proto=socket.IPPROTO_TCP),
            timeout=self.cfg.timeout,
        )
        # Preserve resolver order; it usually encodes preference.
        seen: dict[str, None] = {}
        for info in infos:
            seen.setdefault(info[4][0], None)
        return tuple(seen)

    async def prefetch(self, hosts: list[str]) -> None:
        """Warm the cache for hosts whose back queues are about to come due."""
        await asyncio.gather(*(self.resolve(h) for h in hosts), return_exceptions=True)

    def stats(self) -> dict[str, int]:
        now = time.monotonic()
        live = sum(1 for e in self._cache.values() if e.expires_at > now)
        return {"entries": len(self._cache), "live": live, "inflight": len(self._inflight)}
