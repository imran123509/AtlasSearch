"""Redis-backed URL frontier — the Build implementation of features/URL-FRONTIER.md.

Two queue sets so priority and politeness never compromise each other:

    front queues (ZSET per priority band)   objective: PRIORITY
              │  pop by band, route by host
    back queues (LIST per host)             objective: POLITENESS
              │
    due heap (ZSET scored by nextFetchTime) mediates, keyed ONLY on wall-clock

Invariants enforced here:
  1. a host maps to at most one back queue          (`bq:{domain}` is the queue)
  2. back queues stay non-empty while work exists   (`_refill` on drain)
  3. the heap holds only times, never priorities    (score = next fetch epoch ms)

Leases are at-least-once by design. A duplicate fetch costs one page load;
exactly-once would cost a distributed transaction per URL to prevent exactly that.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import structlog

from .config import Config
from .models import CrawlTask
from .urlnorm import canonicalise, registrable_domain

log = structlog.get_logger(__name__)

_N_BANDS = 8

# Move a host's URL into its back queue and register the host on the due heap,
# but only if the host is not already scheduled — invariant 1.
_ENQUEUE_LUA = """
local bq   = KEYS[1]   -- back queue for this host
local heap = KEYS[2]   -- due heap
local seen = KEYS[3]   -- per-host member set (cheap in-frontier dedup)
local url  = ARGV[1]
local host = ARGV[2]
local now  = tonumber(ARGV[3])
local cap  = tonumber(ARGV[4])

if redis.call('SISMEMBER', seen, url) == 1 then return 0 end
if redis.call('LLEN', bq) >= cap then return -1 end

redis.call('SADD', seen, url)
redis.call('RPUSH', bq, url)
if redis.call('ZSCORE', heap, host) == false then
  redis.call('ZADD', heap, now, host)
end
return 1
"""

# Pop the ripest host, take one URL, and reschedule the host for its next slot.
# If its back queue drains, drop it from the heap so a fetcher is not idled on it.
_LEASE_LUA = """
local heap  = KEYS[1]
local now   = tonumber(ARGV[1])
local delay = tonumber(ARGV[2])   -- ms until this host is due again
local pref  = ARGV[3]

local due = redis.call('ZRANGEBYSCORE', heap, '-inf', now, 'LIMIT', 0, 1)
if #due == 0 then return nil end

local host = due[1]
local bq   = pref .. 'bq:' .. host
local url  = redis.call('LPOP', bq)

if url == false then
  redis.call('ZREM', heap, host)
  return nil
end

if redis.call('LLEN', bq) == 0 then
  redis.call('ZREM', heap, host)
else
  redis.call('ZADD', heap, now + delay, host)
end
return {host, url}
"""


class Frontier:
    def __init__(self, redis: Any, config: Config, *, prefix: str = "fr:") -> None:
        self.redis = redis
        self.cfg = config
        self.p = prefix
        self._enqueue = redis.register_script(_ENQUEUE_LUA)
        self._lease_one = redis.register_script(_LEASE_LUA)

    # -- keys ---------------------------------------------------------------
    def _front(self, band: int) -> str:
        return f"{self.p}front:{band}"

    def _back(self, domain: str) -> str:
        return f"{self.p}bq:{domain}"

    def _seen_in_queue(self, domain: str) -> str:
        return f"{self.p}q:{domain}"

    @property
    def _heap(self) -> str:
        return f"{self.p}due"

    def _lease(self, url: str) -> str:
        return f"lease:{_hash(url)}"

    def _budget(self, domain: str) -> str:
        return f"{self.p}budget:{domain}"

    # -- write --------------------------------------------------------------
    async def add(
        self,
        url: str,
        *,
        priority: int = 500,
        depth: int = 0,
        source_url: str | None = None,
    ) -> bool:
        """Offer a URL to the frontier. Returns False if it was rejected."""
        url = canonicalise(url)
        domain = registrable_domain(url)

        # Per-site URL budget: the single most effective defence against crawl
        # traps, and it works without ever identifying a trap as such.
        spent = await self.redis.incr(self._budget(domain))
        if spent == 1:
            await self.redis.expire(self._budget(domain), 7 * 24 * 3600)
        if spent > await self.site_budget(domain):
            log.debug("frontier.budget_exhausted", domain=domain, spent=spent)
            return False

        band = _band_for(priority)
        await self.redis.zadd(self._front(band), {url: float(priority)})

        res = await self._enqueue(
            keys=[self._back(domain), self._heap, self._seen_in_queue(domain)],
            args=[url, domain, int(time.time() * 1000), 10_000],
        )
        if int(res) == 1 and depth >= 0:
            await self.redis.hset(f"{self.p}meta:{_hash(url)}", mapping={
                "depth": depth,
                "priority": priority,
                "source": source_url or "",
            })
        return int(res) == 1

    async def site_budget(self, domain: str) -> int:
        """base × log(1 + authority). Authority is 0 until PageRank feeds it back."""
        raw = await self.redis.get(f"{self.p}auth:{domain}")
        authority = float(raw) if raw else 0.0
        import math

        return int(500 * math.log1p(1.0 + authority * 1000))

    # -- read ---------------------------------------------------------------
    async def lease(self, count: int = 1) -> list[CrawlTask]:
        """Lease up to `count` due URLs, one per host per call to spread load."""
        tasks: list[CrawlTask] = []
        now_ms = int(time.time() * 1000)

        for _ in range(count):
            rate = self.cfg.politeness.initial_rate
            delay_ms = int(1000 / max(rate, 0.01))
            res = await self._lease_one(keys=[self._heap], args=[now_ms, delay_ms, self.p])
            if not res:
                break

            host = _s(res[0])
            url = _s(res[1])
            token = secrets.token_hex(8)
            await self.redis.set(self._lease(url), token, ex=self.cfg.lease.ttl_seconds)
            await self.redis.srem(self._seen_in_queue(host), url)

            meta = await self.redis.hgetall(f"{self.p}meta:{_hash(url)}")
            validators = await self.redis.hgetall(f"{self.p}val:{_hash(url)}")
            tasks.append(
                CrawlTask(
                    url=url,
                    depth=int(_s(meta.get(b"depth", meta.get("depth", 0))) or 0),
                    priority=int(_s(meta.get(b"priority", meta.get("priority", 500))) or 500),
                    source_url=_s(meta.get(b"source", meta.get("source", ""))) or None,
                    etag=_s(validators.get(b"etag", validators.get("etag", ""))) or None,
                    last_modified=_s(
                        validators.get(b"last_modified", validators.get("last_modified", ""))
                    )
                    or None,
                    lease_token=token,
                )
            )
        return tasks

    async def release(self, task: CrawlTask) -> None:
        """Release a lease we completed. Crash instead → TTL expiry does this."""
        if task.lease_token is None:
            return
        # Only delete our own lease; a expired-and-reissued lease belongs to someone else.
        current = await self.redis.get(self._lease(task.url))
        if current is not None and _s(current) == task.lease_token:
            await self.redis.delete(self._lease(task.url))

    async def requeue(self, task: CrawlTask, *, delay_s: float = 0.0) -> None:
        """Put a URL back, optionally after a delay (5xx decay, rate-limit deferral)."""
        await self.release(task)
        domain = registrable_domain(task.url)
        due = int((time.time() + delay_s) * 1000)
        await self.redis.rpush(self._back(domain), task.url)
        await self.redis.sadd(self._seen_in_queue(domain), task.url)
        await self.redis.zadd(self._heap, {domain: due})

    async def store_validators(self, url: str, *, etag: str | None, last_modified: str | None) -> None:
        """Remember conditional-GET validators so the next fetch can be a 304."""
        mapping = {k: v for k, v in (("etag", etag), ("last_modified", last_modified)) if v}
        if mapping:
            await self.redis.hset(f"{self.p}val:{_hash(url)}", mapping=mapping)

    async def stats(self) -> dict[str, int]:
        return {
            "hosts_scheduled": int(await self.redis.zcard(self._heap) or 0),
            "hosts_due": int(
                await self.redis.zcount(self._heap, "-inf", int(time.time() * 1000)) or 0
            ),
        }


def _band_for(priority: int) -> int:
    return max(0, min(_N_BANDS - 1, priority * _N_BANDS // 1000))


def _hash(url: str) -> str:
    import hashlib

    return hashlib.blake2b(url.encode(), digest_size=8).hexdigest()


def _s(v: Any) -> str:
    if v is None:
        return ""
    return v.decode() if isinstance(v, bytes) else str(v)
