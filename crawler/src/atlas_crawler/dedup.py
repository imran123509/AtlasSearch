"""URL de-duplication: the seen-URL test, batching, site budgets, trap detection.

Canonicalisation lives in `urlnorm.py` — it is the other half of this feature and
is a pure function, so it is tested separately.

The central decision here is Option C from features/URL-DE-DUPLICATION.md:

    in-memory Bloom  →  "definitely new"  →  skip the lookup, insert
                     →  "maybe seen"      →  consult the exact store

A Bloom filter *alone* is the trap. Its false positives mean a URL is
**permanently never crawled**, silently, with no way to detect it from inside the
system. In the hybrid the same false positive costs one lookup instead of one
document, because the exact store has the final say. Bloom filters have no false
negatives, which is the property that makes this safe: "definitely new" really is
definite.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Protocol
from urllib.parse import urlsplit

import structlog

from .urlnorm import canonicalise, registrable_domain

log = structlog.get_logger(__name__)

_MASK64 = (1 << 64) - 1


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------

def fingerprint(url: str) -> int:
    """64-bit fingerprint of a *canonical* URL.

    At 5x10^11 URLs the probability of at least one 64-bit collision across the
    whole corpus is ~2.7%, and a collision costs one shadowed page. That is
    acceptable; move to 128 bits (and 8 TB instead of 4 TB) if the corpus grows
    past ~10^12.
    """
    return int.from_bytes(hashlib.blake2b(url.encode(), digest_size=8).digest(), "big")


def _double_hash(fp: int) -> tuple[int, int]:
    """Kirsch-Mitzenmacher: derive k hashes from two, with no loss in fp rate."""
    h2 = int.from_bytes(
        hashlib.blake2b(fp.to_bytes(8, "big"), digest_size=8).digest(), "big"
    )
    return fp, (h2 | 1)  # odd h2 keeps the stride coprime with a power-of-two m


def bloom_params(capacity: int, error_rate: float = 0.01) -> tuple[int, int]:
    """Return (bits, hash_count) for a Bloom filter of `capacity` items."""
    if capacity <= 0:
        raise ValueError("capacity must be positive")
    bits = int(math.ceil(-capacity * math.log(error_rate) / (math.log(2) ** 2)))
    k = max(1, int(round(bits / capacity * math.log(2))))
    return bits, k


# ---------------------------------------------------------------------------
# Bloom filter — the negative filter
# ---------------------------------------------------------------------------

_BLOOM_TEST_AND_SET = """
-- Returns 1 if every bit was ALREADY set (maybe seen), 0 if any was clear
-- (definitely new). Sets all bits either way, so this is one atomic round trip.
local all_set = 1
for i = 1, #ARGV do
  if redis.call('GETBIT', KEYS[1], ARGV[i]) == 0 then
    all_set = 0
  end
end
for i = 1, #ARGV do
  redis.call('SETBIT', KEYS[1], ARGV[i], 1)
end
return all_set
"""


class RedisBloom:
    """Bloom filter over Redis bitmaps.

    Sharded across several keys because a Redis string caps at 512 MB, and a
    single bitmap for a real corpus is far larger than that.
    """

    def __init__(
        self,
        redis: Any,
        *,
        capacity: int = 10_000_000,
        error_rate: float = 0.01,
        shards: int = 8,
        prefix: str = "seen:bloom",
    ) -> None:
        self.redis = redis
        self.prefix = prefix
        self.shards = shards
        self.bits, self.hashes = bloom_params(capacity, error_rate)
        self.bits_per_shard = max(1, self.bits // shards)
        self._script = redis.register_script(_BLOOM_TEST_AND_SET)

    def _positions(self, fp: int) -> tuple[str, list[int]]:
        h1, h2 = _double_hash(fp)
        shard = (h1 >> 32) % self.shards
        return (
            f"{self.prefix}:{shard}",
            [((h1 + i * h2) & _MASK64) % self.bits_per_shard for i in range(self.hashes)],
        )

    async def test_and_add(self, fp: int) -> bool:
        """True = *maybe* seen (consult the exact store). False = definitely new."""
        key, positions = self._positions(fp)
        return bool(int(await self._script(keys=[key], args=positions)))

    async def contains(self, fp: int) -> bool:
        key, positions = self._positions(fp)
        pipe = self.redis.pipeline()
        for pos in positions:
            pipe.getbit(key, pos)
        return all(bool(b) for b in await pipe.execute())

    async def add(self, fp: int) -> None:
        key, positions = self._positions(fp)
        pipe = self.redis.pipeline()
        for pos in positions:
            pipe.setbit(key, pos, 1)
        await pipe.execute()


# ---------------------------------------------------------------------------
# Exact store — the authority
# ---------------------------------------------------------------------------

class ExactStore(Protocol):
    async def contains_many(self, fps: list[int]) -> set[int]: ...
    async def add_many(self, fps: list[int]) -> None: ...


class RedisExactStore:
    """Fingerprints in Redis sets, sharded by the high bits of the fingerprint.

    Sharding on the fingerprint (not the host) keeps shards evenly sized: host
    distribution is Zipfian and would put a tenth of the web in one shard.
    """

    def __init__(self, redis: Any, *, shards: int = 64, prefix: str = "seen:exact") -> None:
        self.redis = redis
        self.shards = shards
        self.prefix = prefix

    def _key(self, fp: int) -> str:
        return f"{self.prefix}:{(fp >> 40) % self.shards}"

    async def contains_many(self, fps: list[int]) -> set[int]:
        if not fps:
            return set()
        # Group by shard so each shard is touched exactly once — this is the
        # batching that turns random I/O into a sequential sweep.
        by_shard: dict[str, list[int]] = {}
        for fp in fps:
            by_shard.setdefault(self._key(fp), []).append(fp)

        pipe = self.redis.pipeline()
        ordered: list[tuple[str, list[int]]] = []
        for key, group in by_shard.items():
            ordered.append((key, group))
            pipe.smismember(key, [str(fp) for fp in group])
        results = await pipe.execute()

        present: set[int] = set()
        for (_key, group), flags in zip(ordered, results):
            for fp, flag in zip(group, flags):
                if flag:
                    present.add(fp)
        return present

    async def add_many(self, fps: list[int]) -> None:
        if not fps:
            return
        by_shard: dict[str, list[str]] = {}
        for fp in fps:
            by_shard.setdefault(self._key(fp), []).append(str(fp))
        pipe = self.redis.pipeline()
        for key, group in by_shard.items():
            pipe.sadd(key, *group)
        await pipe.execute()


class InMemoryExactStore:
    """For tests and single-process runs."""

    def __init__(self) -> None:
        self._seen: set[int] = set()

    async def contains_many(self, fps: list[int]) -> set[int]:
        return self._seen & set(fps)

    async def add_many(self, fps: list[int]) -> None:
        self._seen.update(fps)

    def __len__(self) -> int:
        return len(self._seen)


# ---------------------------------------------------------------------------
# The hybrid
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SeenStats:
    checked: int = 0
    bloom_skipped: int = 0   # "definitely new" — no exact lookup needed
    exact_consulted: int = 0
    already_seen: int = 0

    @property
    def bloom_absorption(self) -> float:
        """Share of lookups the Bloom filter answered alone. Doc expects ~85%."""
        return self.bloom_skipped / self.checked if self.checked else 0.0


class SeenUrlFilter:
    """Hybrid Bloom + exact store. The exact store always has the final say."""

    def __init__(self, bloom: RedisBloom | None, exact: ExactStore) -> None:
        self.bloom = bloom
        self.exact = exact
        self.stats = SeenStats()

    async def filter_new(self, urls: Iterable[str]) -> list[str]:
        """Return the URLs never seen before, and record them as seen.

        Batched deliberately: point lookups against a 4 TB store are the
        bottleneck, not the storage. Accumulate, sort by fingerprint, sweep each
        shard once. A few minutes of buffer latency is irrelevant — the frontier
        schedules hours ahead.
        """
        canonical: dict[int, str] = {}
        for url in urls:
            try:
                c = canonicalise(url)
            except Exception:  # noqa: BLE001 - a malformed URL is data, not a crash
                continue
            canonical.setdefault(fingerprint(c), c)

        if not canonical:
            return []

        self.stats.checked += len(canonical)

        # Sorting turns the store sweep from random access into a sequential one.
        # It is also what makes the shard grouping below contiguous.
        fps = sorted(canonical)

        maybe_seen: list[int] = []
        if self.bloom is not None:
            for fp in fps:
                if await self.bloom.test_and_add(fp):
                    maybe_seen.append(fp)
            self.stats.bloom_skipped += len(fps) - len(maybe_seen)
        else:
            maybe_seen = fps

        # Only the Bloom's "maybe" set reaches the exact store.
        self.stats.exact_consulted += len(maybe_seen)
        present = await self.exact.contains_many(maybe_seen) if maybe_seen else set()
        self.stats.already_seen += len(present)

        new_fps = [fp for fp in fps if fp not in present]
        await self.exact.add_many(new_fps)
        return [canonical[fp] for fp in new_fps]

    async def is_new(self, url: str) -> bool:
        """Single-URL convenience. Prefer `filter_new` — batching is the point."""
        return bool(await self.filter_new([url]))


# ---------------------------------------------------------------------------
# Per-site URL budget
# ---------------------------------------------------------------------------

class SiteBudget:
    """`base x log(1 + authority)`.

    The single most effective defence against crawl traps, and it works without
    ever identifying a trap as such: infinite calendars, faceted-navigation
    explosions and session-ID generators all hit the ceiling and stop, whatever
    produced them.
    """

    def __init__(self, redis: Any, *, base: int = 500, ttl: int = 7 * 24 * 3600,
                 prefix: str = "dedup") -> None:
        self.redis = redis
        self.base = base
        self.ttl = ttl
        self.p = prefix

    def _spent_key(self, domain: str) -> str:
        return f"{self.p}:budget:{domain}"

    def _auth_key(self, domain: str) -> str:
        return f"{self.p}:auth:{domain}"

    async def limit_for(self, domain: str) -> int:
        raw = await self.redis.get(self._auth_key(domain))
        authority = float(raw) if raw else 0.0
        return int(self.base * math.log1p(1.0 + authority * 1000))

    async def set_authority(self, domain: str, authority: float) -> None:
        await self.redis.set(self._auth_key(domain), authority)

    async def exhausted(self, domain: str) -> bool:
        """Read-only check, so a caller can test the budget before committing.

        This exists to keep the seen-URL filter and the budget from interacting
        badly. Marking a URL seen and *then* rejecting it on budget loses it
        permanently — the budget counter resets weekly, but a Bloom filter has
        no un-see operation. Callers must therefore check `exhausted()` first
        and `spend()` only once the URL is actually being queued.
        """
        return await self.spent(domain) >= await self.limit_for(domain)

    async def spend(self, domain: str, n: int = 1) -> bool:
        """Charge `n` URLs against the site's budget. False = now exhausted."""
        spent = await self.redis.incrby(self._spent_key(domain), n)
        if spent == n:
            await self.redis.expire(self._spent_key(domain), self.ttl)
        return spent <= await self.limit_for(domain)

    async def spent(self, domain: str) -> int:
        raw = await self.redis.get(self._spent_key(domain))
        return int(raw) if raw else 0


# ---------------------------------------------------------------------------
# Trap detection — the second layer
# ---------------------------------------------------------------------------

_TRAILING_ID = re.compile(r"^\d+$|^[0-9a-f]{8,}$", re.I)


def path_prefixes(url: str, *, max_depth: int = 4) -> list[str]:
    """Path prefixes to attribute a URL to, e.g. `/calendar`, `/calendar/2026`.

    Numeric and hex segments are collapsed to `*` so that `/calendar/2026/03`
    and `/calendar/2027/04` are recognised as the same *pattern*. Demoting the
    pattern is the point — demoting individual URLs is whack-a-mole against a
    generator that produces them faster than you can act.
    """
    parts = urlsplit(url)
    host = registrable_domain(url) or (parts.hostname or "")
    segments = [s for s in parts.path.split("/") if s][:max_depth]
    out: list[str] = []
    acc = ""
    for seg in segments:
        acc += "/" + ("*" if _TRAILING_ID.match(seg) else seg)
        out.append(f"{host}{acc}")
    return out


@dataclass(slots=True)
class TrapVerdict:
    is_trap: bool
    prefix: str | None = None
    urls: int = 0
    distinct_content: int = 0

    @property
    def ratio(self) -> float:
        return self.distinct_content / self.urls if self.urls else 1.0


class TrapDetector:
    """Track content-hash diversity per path prefix.

    A path pattern producing many URLs but few distinct content hashes is a
    generator, not a section of a website. Distinct hashes are counted with a
    HyperLogLog: exact cardinality would cost as much memory as the trap itself.
    """

    def __init__(
        self,
        redis: Any,
        *,
        min_urls: int = 1_000,
        max_distinct_ratio: float = 0.05,
        ttl: int = 30 * 24 * 3600,
        prefix: str = "dedup:trap",
    ) -> None:
        self.redis = redis
        self.min_urls = min_urls
        self.max_distinct_ratio = max_distinct_ratio
        self.ttl = ttl
        self.p = prefix

    def _count_key(self, prefix: str) -> str:
        return f"{self.p}:n:{prefix}"

    def _hll_key(self, prefix: str) -> str:
        return f"{self.p}:hll:{prefix}"

    def _demoted_key(self, prefix: str) -> str:
        return f"{self.p}:demoted:{prefix}"

    async def observe(self, url: str, content_hash: str) -> TrapVerdict:
        """Record one fetched URL and its content hash; report on its prefixes."""
        worst = TrapVerdict(is_trap=False)
        for prefix in path_prefixes(url):
            pipe = self.redis.pipeline()
            pipe.incr(self._count_key(prefix))
            pipe.expire(self._count_key(prefix), self.ttl)
            pipe.pfadd(self._hll_key(prefix), content_hash)
            pipe.expire(self._hll_key(prefix), self.ttl)
            pipe.pfcount(self._hll_key(prefix))
            results = await pipe.execute()
            urls, distinct = int(results[0]), int(results[4])

            if urls >= self.min_urls and distinct / urls < self.max_distinct_ratio:
                await self.redis.set(self._demoted_key(prefix), 1, ex=self.ttl)
                verdict = TrapVerdict(True, prefix, urls, distinct)
                log.info(
                    "dedup.trap_detected",
                    prefix=prefix, urls=urls, distinct=distinct,
                    ratio=round(verdict.ratio, 4),
                )
                return verdict
            worst = TrapVerdict(False, prefix, urls, distinct)
        return worst

    async def is_demoted(self, url: str) -> bool:
        """Whether any prefix of this URL has been demoted as a generator."""
        prefixes = path_prefixes(url)
        if not prefixes:
            return False
        pipe = self.redis.pipeline()
        for prefix in prefixes:
            pipe.exists(self._demoted_key(prefix))
        return any(bool(x) for x in await pipe.execute())

    async def demote(self, prefix: str) -> None:
        await self.redis.set(self._demoted_key(prefix), 1, ex=self.ttl)

    async def undemote(self, prefix: str) -> None:
        await self.redis.delete(self._demoted_key(prefix))
