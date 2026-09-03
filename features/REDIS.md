# Redis

Shared, low-latency mutable state: rate limiters, dedup filters, caches, and coordination.
Everything that must be **fast, shared, and is acceptable to lose**.

That last clause is the design rule. Nothing in Redis may be the only copy of something the
system needs to be correct.

Related: [WEB-CRAWLER](WEB-CRAWLER.md) · [URL-DE-DUPLICATION](URL-DE-DUPLICATION.md) · [SEARCH-ENGINE](SEARCH-ENGINE.md)

---

## What lives here

| Use | Structure | Key | TTL | Loss impact |
| --- | --- | --- | --- | --- |
| Host rate limiter | Hash (token bucket) | `rl:host:{domain}` | 1 h | Brief over-crawl |
| IP rate limiter | Hash | `rl:ip:{ip}` | 1 h | Brief over-crawl |
| robots.txt cache | String | `robots:{scheme}:{host}` | 4 h | Refetch |
| Seen-URL filter | Bloom / bitmap | `seen:{shard}` | none | **Re-crawl storm** — see below |
| Results cache | String (compressed JSON) | `sc:{gen}:{hash(q,locale,safe)}` | 5 m–24 h | 2.2× backend load |
| Snippet cache | String | `sn:{doc_id}:{qhash}` | until gen flip | Slower p99 |
| API rate limiter | Hash | `arl:{class}:{id}` | 1 m | Brief over-serve |
| Crawl leases | String + TTL | `lease:{url_hash}` | 5 m | Duplicate fetch |
| Deny-list overlay | Set | `deny:{gen}` | until gen flip | **Legal exposure** |

---

## Token bucket — must be atomic

The read-modify-write in a rate limiter is a classic race. Two fetchers reading `tokens=1`
simultaneously both proceed, and politeness is broken. Do it in Lua, which Redis runs
atomically:

```lua
-- KEYS[1] = bucket key
-- ARGV = now_ms, rate_per_sec, burst, cost
local b    = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local now  = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local burst= tonumber(ARGV[3])
local cost = tonumber(ARGV[4])

local tokens = tonumber(b[1]) or burst
local ts     = tonumber(b[2]) or now

tokens = math.min(burst, tokens + (now - ts) / 1000.0 * rate)

if tokens < cost then
  redis.call('HMSET', KEYS[1], 'tokens', tokens, 'ts', now)
  redis.call('EXPIRE', KEYS[1], 3600)
  return {0, math.ceil((cost - tokens) / rate * 1000)}   -- denied, retry_after_ms
end

redis.call('HMSET', KEYS[1], 'tokens', tokens - cost, 'ts', now)
redis.call('EXPIRE', KEYS[1], 3600)
return {1, 0}                                            -- allowed
```

Both `(domain, ip)` buckets must be checked. Check the cheaper/more-likely-to-deny one first
and short-circuit — usually the domain bucket.

---

## Results cache

```
key   = sc:{index_generation}:{sha1(normalised_q | locale | safe | freshness)}
value = zstd(json)
ttl   = by intent: 60 s trending … 24 h navigational
```

### The generation in the key is load-bearing

It means a new index generation makes old entries **unreachable** rather than requiring a
flush. The cache ages out gradually instead of dropping to zero in one step.

That step would be fatal: 55% → 0% hit rate is a **2.2× step in backend load**, exceeding
capacity ([SEARCH-ENGINE](SEARCH-ENGINE.md)). Set `maxmemory-policy allkeys-lru` so the stale
generation's entries are evicted naturally as the new generation fills.

### Admission — cache on second sight

~15% of daily queries are never seen again. Admitting them on first sight fills the cache
with entries nobody reads.

```
if not SETNX("seen1:" + qhash, 1, ex=600):   # second occurrence within 10 min
    cache the result
```

### Never cache degraded results

```python
if resp.meta.partial or resp.meta.degraded:
    return resp          # do not write to cache
```

One bad minute otherwise poisons the cache for hours.

---

## Seen-URL filter — the one dangerous entry

A Bloom filter here has a false-positive rate, and a false positive means a URL is
**permanently never crawled, silently**. See [URL-DE-DUPLICATION](URL-DE-DUPLICATION.md) —
Redis holds only the *negative filter*, and the authoritative store is Postgres or the
sharded exact store.

```
Bloom says "definitely new"  → skip the exact lookup, insert         (~85% of traffic)
Bloom says "maybe seen"      → consult the authoritative store
```

**Losing this key is not data loss, it is a re-crawl storm** — everything looks new at once.
Rate-limit re-discovery after a Redis restart, and warm the filter from the authoritative
store before resuming crawl at full rate.

---

## Operational configuration

```conf
maxmemory 24gb
maxmemory-policy allkeys-lru        # caches evict; see below for the exception

appendonly yes                      # AOF for leases and limiters
appendfsync everysec                # fsync-per-write is not worth it here
save ""                             # no RDB snapshots — AOF is enough

timeout 300
tcp-keepalive 60
```

### Separate instances, not one shared

`allkeys-lru` will happily evict a crawl lease or a deny-list entry to make room for a cached
SERP. Split by eviction policy:

| Instance | Policy | Contents |
| --- | --- | --- |
| `redis-cache` | `allkeys-lru` | Results, snippets, robots |
| `redis-state` | `noeviction` | Leases, limiters, deny-list, Bloom |

Under memory pressure `redis-state` returns errors, which is correct — an error is
recoverable, a silently evicted deny-list entry is a legal problem.

---

## Client rules

- **Always set a TTL.** A key without one is a leak. Enforce this in a wrapper, not by
  convention.
- **Never `KEYS`.** It blocks the single-threaded server. Use `SCAN` with a cursor.
- **Pipeline batches.** A per-URL round trip caps the crawler at ~10 K ops/s per connection.
- **Set client timeouts short** (50 ms). Redis is on the critical path; a slow Redis must
  degrade to "cache miss", never to "request hangs".

```python
try:
    cached = await redis.get(key, timeout=0.05)
except (TimeoutError, ConnectionError):
    cached = None          # degrade to a miss, always
```

---

## Target vs Build

| | Target | Build |
| --- | --- | --- |
| Rate limiters | **In-process**, sharded by host | Redis Lua |
| Results cache | Sharded in-memory service + CDN | Redis |
| Seen-URL | Own sharded exact store | Redis Bloom + Postgres |
| Topology | — | Single instance, or Sentinel |

At target scale the rate limiter **must** move in-process: a Redis round trip per fetch
decision does not survive 10 K fetches/s, and [Kafka domain-keyed partitioning](KAFKA.md)
already gives each consumer exclusive ownership of its hosts, which removes the need for
shared state. That is the migration.

---

## Failure modes

| Failure | Consequence | Mitigation |
| --- | --- | --- |
| Non-atomic rate limiter | Politeness violated; you get blocked | Lua script |
| Redis down, no fallback | Crawler or API stops entirely | Degrade to miss; fail-closed on limiters (do not crawl) |
| Cache and state share an instance | Lease or deny-list entry evicted under pressure | Separate instances by eviction policy |
| Bloom filter lost | Re-crawl storm | Warm from authoritative store; rate-limit re-discovery |
| No TTL | Unbounded memory growth | Enforce TTL in the client wrapper |
| `KEYS` in production | Server blocks; everything times out | `SCAN` only |
| Degraded results cached | Bad minute poisons hours | Check `partial`/`degraded` before writing |
| Long client timeout | Redis latency becomes API latency | 50 ms cap, degrade to miss |
