# Web Crawler

Fetches pages from the open web, politely, at a budget set by how much the rest of the
pipeline can absorb.

Related: [URL-FRONTIER](URL-FRONTIER.md) · [DISTRIBUTED-CRAWLER](DISTRIBUTED-CRAWLER.md) · [HTML-PARSER](HTML-PARSER.md) · [URL-DE-DUPLICATION](URL-DE-DUPLICATION.md)

---

## The constraint everyone gets wrong

The instinct is to size a crawler on bandwidth. That is wrong by an order of magnitude.

Target-scale arithmetic:

| Source | Rate |
| --- | --- |
| 250 M tier-0 docs, daily refresh | 250 M/day |
| 1.75 B tier-1 docs, weekly refresh | 250 M/day |
| 8 B tier-2 docs, 45-day refresh | 178 M/day |
| New-URL discovery and validation | 200 M/day |
| **Total** | **~880 M/day ≈ 10,200 fetches/s** |

Now the bytes. Roughly 60% of refresh fetches return `304 Not Modified` against an `ETag`
or `If-Modified-Since`, costing ~1 KB each. Only ~400 M/day are full bodies:

```
400 M × 90 KB = 36 TB/day ≈ 3.3 Gbit/s mean, under 10 Gbit/s peak
```

Ten gigabits is a rounding error. **The binding constraints are, in order:**

1. **Per-host politeness capacity.** Crawl capacity is the sum over hosts of what each host
   tolerates. The pages worth having concentrate on a few million hosts that each cap you at
   one or two concurrent connections. A bigger pipe buys nothing.
2. **DNS resolution throughput.** At 10 K fetches/s an uncached resolver is a second
   full-scale distributed system you did not plan for.
3. **Index build throughput.** You cannot crawl faster than you can index.

---

## Politeness

### Key on both domain and IP

Rate limiting on hostname alone is wrong: one shared-hosting IP can serve tens of thousands
of virtual hosts, and crawling them "politely" in parallel still melts one machine. IP alone
is also wrong: a CDN-fronted site presents a handful of IPs for millions of pages you are
entitled to crawl faster.

```
limiter_key = (registrable_domain, resolved_ip)
```

Both must have budget before a fetch is issued.

### Adaptive rate, not a constant

```
start:      1 request / second / host
on 200:     additive increase, capped by declared Crawl-delay
on 429/503: multiplicative backoff, honour Retry-After exactly
on latency rise: back off before the host starts erroring
```

AIMD. A host that stays healthy earns more throughput; a host under strain gets relief
before it returns errors.

### robots.txt

- Cache with a TTL of a few hours, keyed by scheme + host + port.
- **Fail closed.** An unfetchable `robots.txt` means *do not crawl*, not *crawl freely*.
  This is the single most common correctness bug in hobby crawlers.
- Honour `Crawl-delay` where declared; honour `Retry-After` always.
- Re-check on a stale TTL before the first fetch of a new session against that host.

---

## Fetch loop

```
lease URL batch from frontier      (see URL-FRONTIER.md)
  → check robots cache             (fetch + cache on miss)
  → check (domain, ip) rate limits (see REDIS.md)
  → conditional GET with stored ETag / Last-Modified
  → on 200:  write body to blob store, emit to fetched topic
    on 304:  update crawl metadata only, no body transfer
    on 3xx:  record redirect, canonicalise, requeue target
    on 4xx:  record, demote or drop by class
    on 5xx:  backoff, retry with decay, give up after N
  → release lease
```

### Response handling detail

| Code | Action |
| --- | --- |
| 200 | Store body. Emit `pages.fetched`. Update `ETag`, `Last-Modified`, content hash. |
| 301/308 | Record permanent redirect. Rewrite canonical. Do not keep the source URL indexed. |
| 302/307 | Follow, but keep the source URL as canonical. |
| 304 | Cheapest possible outcome. Update `last_verified`, leave the document as-is. |
| 404/410 | Tombstone the document. 410 removes immediately; 404 after N confirmations. |
| 429/503 | Back off the whole host, not just this URL. Honour `Retry-After`. |
| Timeout | Count against host health. Three consecutive → halve the host's rate. |

---

## DNS

Run your own recursive resolvers with a large cache and prefetching. Public resolvers will
rate-limit you at this volume, and their latency variance leaks straight into fetch p99.

- Cache with respect for TTL, but floor it — some sites publish 30-second TTLs.
- Prefetch resolution for hosts whose back queues are about to become due.
- Cache negative results too, with a shorter TTL.

---

## Target vs Build

| | Target | Build |
| --- | --- | --- |
| Rate | 10,200 fetches/s | 20–50 fetches/s |
| Fetcher | Custom async pool, host-pinned | Python `httpx` / `aiohttp`, asyncio |
| DNS | Own recursive resolvers | System resolver + local cache |
| Rate limits | In-process, sharded by host | Redis token buckets ([REDIS](REDIS.md)) |
| Rendering | 25% of fetches, own browser farm | Allow-listed domains, Playwright |
| Output | Blob store + internal queue | S3/MinIO + Kafka ([KAFKA](KAFKA.md)) |

The Build differs mostly in *where the rate limiter state lives*. At target scale it must be
in-process (a Redis round-trip per fetch decision does not survive 10 K/s); at build scale
Redis is simpler and fast enough.

---

## Failure modes

| Failure | Consequence | Mitigation |
| --- | --- | --- |
| Fetcher process dies | In-flight URLs lost | Lease expiry returns them to the frontier. At-least-once; duplicate fetches are harmless and cheaper than exactly-once coordination. |
| robots.txt fetch fails | Either over-crawl or under-crawl | Fail closed. Alert if the closed-fail rate exceeds a threshold — it usually means a DNS problem, not a robots problem. |
| Host starts tarpitting | Fetcher threads block indefinitely | Hard per-request deadline. A slow host must not consume a thread slot. |
| Redirect loop | Infinite fetch cycle | Chain depth limit (5), plus cycle detection on the URL set. |
| Crawl trap | Unbounded URL discovery on one host | Per-site URL budget proportional to authority. See [URL-DE-DUPLICATION](URL-DE-DUPLICATION.md). |
| Politeness bug | You get blocked, or sued | Integration test asserting no two in-flight requests share a limiter key. This is worth a test. |

---

## Ethics and legality

Not optional, and not a footnote:

- Honour `robots.txt` and `<meta name="robots">`. Both.
- Send a truthful `User-Agent` with a contact URL explaining the crawler and how to block it.
- Provide a working opt-out and respond to it within a day.
- Never crawl behind authentication or paywalls, and never evade rate limiting by rotating IPs.
- Respect `noarchive` (do not store a cached copy) and `nosnippet` (do not show extracted text).

A crawler that evades blocks is not a crawler, it is an attack. The design above is
deliberately structured so that politeness is enforced in one place and cannot be bypassed
by a caller.
