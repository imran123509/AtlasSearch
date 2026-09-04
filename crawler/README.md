# crawler

Implementation of [features/WEB-CRAWLER.md](../features/WEB-CRAWLER.md) — Build layer.

```
lease URL from frontier
  → resolve DNS                  also gives us half the politeness key
  → check robots                 fail closed
  → check (domain, ip) budget    atomic, both keys, one Lua script
  → conditional GET, hard deadline
  → classify per the response table
  → feed the AIMD controller
  → body → blob store,  pointer → Kafka
  → release lease
```

## Modules

| Module | Responsibility |
| --- | --- |
| `fetcher.py` | The fetch loop and the response-handling table |
| `ratelimit.py` | Paired token buckets + AIMD. **The only place politeness is enforced.** |
| `robots.py` | robots.txt fetch/cache/evaluate, `X-Robots-Tag` |
| `dedup.py` | Seen-URL filter (Bloom + exact), site budgets, trap detection |
| `frontier.py` | Front/back queues, due heap, leases |
| `dns.py` | Resolution cache with negative caching and request collapsing |
| `urlnorm.py` | Canonicalisation (pure, idempotent) |
| `storage.py` | Blob store — bodies go here |
| `emit.py` | Kafka — only *pointers* go here |
| `main.py` | Worker wiring, outcome handling, graceful drain |

## Run it

```bash
python -m venv .venv && . .venv/Scripts/activate      # or bin/activate
pip install -e ".[dev]"

# Dry run: local blobs, no Kafka, no S3. Needs Redis on localhost:6379.
export CRAWL_USER_AGENT="YourBot/0.1 (+https://your.site/bot; you@your.site)"
atlas-crawler --dry-run --seed https://example.com/ --metrics-port 9100
```

`Config.validate()` refuses to start if `CRAWL_USER_AGENT` has no `+http` contact
URL. That is deliberate — a crawler site operators cannot identify or contact is
not one that should be running.

## Test

```bash
pytest -q          # 121 tests, no network, no Redis (fakeredis + respx)
```

Tests that exist for a specific reason:

| Test | Guards |
| --- | --- |
| `test_politeness.py::test_no_two_in_flight_requests_share_a_host` | The core invariant. A politeness bug surfaces nowhere else until a site operator complains. |
| `test_politeness.py::test_shared_hosting_ip_is_also_rate_limited` | Why the key is `(domain, IP)`. Hostname alone lets 12 vhosts on one machine be hit at once while every per-host limit looks respected. |
| `test_robots.py::test_fails_closed_on_unreachable` | The most common correctness bug in hobby crawlers. |
| `test_ratelimit.py::test_denial_does_not_deduct_from_the_other_bucket` | Why both buckets are in one Lua script — otherwise tokens leak and good hosts starve. |
| `test_urlnorm.py::test_canonicalise_is_idempotent` | A non-idempotent canonicaliser silently defeats de-duplication. |
| `test_frontier.py::test_release_does_not_steal_a_reissued_lease` | After a lease expires and is reissued, the old holder must not free it. |
| `test_dedup.py::test_a_pathological_bloom_loses_nothing` | The hybrid must survive a 100%-false-positive Bloom. This is what makes Option C safe and Option A a trap. |
| `test_frontier.py::test_a_released_url_cannot_be_requeued_forever` | The in-queue set is deleted on lease; only the global filter stops endless re-crawling. |
| `test_frontier.py::test_budget_exhaustion_does_not_permanently_lose_urls` | Seen-marking is irreversible, budget is not — so the order they run in decides whether a URL is lost. |

## Two decisions that differ from the naive reading

**Crawl-delay is taken as the max of our group and `*`.** Robots semantics say the
most specific matching group applies exclusively — so a site with `Crawl-delay: 5`
under `User-agent: *` *and* a group naming our bot would, read strictly, impose no
delay on us. That is the aggressive reading of an operator plainly asking for
slower crawling. See `RobotsRules.crawl_delay`.

**401/403 on robots.txt fails closed.** RFC 9309 classes 4xx as "unavailable"
(allow all). A server demanding credentials for robots.txt is signalling
deliberate access control, and crawling it anyway is not defensible. 404/410 still
allow, per spec.

## De-duplication: three doors into the frontier

[URL-DE-DUPLICATION.md](../features/URL-DE-DUPLICATION.md) is implemented across
`urlnorm.py` (canonicalisation) and `dedup.py` (everything else). The seen-URL test
is the hybrid — Bloom as a *negative* filter in front of an exact store:

```
Bloom "definitely new"  → skip the lookup, insert          ~90% of traffic
Bloom "maybe seen"      → the exact store has the final say
```

A Bloom filter alone is the trap the doc warns about: its false positives mean a URL
is **permanently never crawled**, silently. In the hybrid the same false positive
costs one lookup instead of one document. `test_a_pathological_bloom_loses_nothing`
proves this by running the filter with a Bloom that returns "maybe" for *everything*.

Because the filter is irreversible, the frontier has three distinct entry points:

| Method | Seen filter | Budget | Use |
| --- | --- | --- | --- |
| `add_many(urls)` | enforced | charged | Discovery — links found while parsing |
| `schedule_recrawl(url)` | **bypassed** | not charged | Refresh — the periodic re-fetch |
| `requeue(task)` | **bypassed** | not charged | Retry — 5xx decay, rate-limit deferral |

Prefer `add_many`. Batching is the whole point: point lookups against a 4 TB store are
the bottleneck, so URLs are accumulated, sorted by fingerprint, and swept once per shard.

## Redirects cost politeness budget

Every hop is a real HTTP request, so every hop spends a token. But a redirect
chain is *one logical fetch*: abandoning it because hop 2 has no token yet means
the retry lands on the same wall and a multi-hop chain on a slow host can never
complete. So the first hop defers if the host is busy, and **mid-chain hops wait**
for their slot, bounded by the request deadline. A host paused by a 429 is never
waited on inline — that backoff is measured in minutes.

## Environment

| Variable | Default | Notes |
| --- | --- | --- |
| `CRAWL_USER_AGENT` | — | **Required.** Must contain a `+http…` contact URL. |
| `CRAWL_INITIAL_RATE` | `1.0` | req/s per host, before AIMD adjusts |
| `CRAWL_MAX_RATE` | `5.0` | ceiling AIMD may earn |
| `CRAWL_BURST` | `2` | token bucket burst |
| `CRAWL_CONCURRENCY` | `32` | in-flight fetches per worker |
| `CRAWL_TIMEOUT` | `20.0` | hard per-request deadline (tarpit defence) |
| `CRAWL_MAX_BODY` | `10485760` | body cap, enforced mid-stream |
| `ROBOTS_TTL` | `14400` | robots cache TTL |
| `REDIS_URL` / `KAFKA_BROKERS` / `S3_ENDPOINT` / `S3_BUCKET` | localhost | |

## Not implemented here

- **Rendering** — headless browser pool. Separate service; memory-bound, and it
  executes untrusted code, so it gets its own pool and NetworkPolicy
  ([KUBERNETES](../features/KUBERNETES.md)).
- **Link extraction** — belongs to [HTML-PARSER](../features/HTML-PARSER.md).
  This crawler emits `pages.fetched`; the parser discovers URLs and feeds them
  back to `Frontier.add`.
- **Content de-duplication** — [CONTENT-DE-DUPLICATION](../features/CONTENT-DE-DUPLICATION.md),
  runs at index build because it needs a global shuffle.
- **In-process rate limiting** — the Target migration. Redis works to roughly
  1K fetches/s; past that the round trip per fetch decision dominates and
  ownership moves to Kafka partition assignment
  ([DISTRIBUTED-CRAWLER](../features/DISTRIBUTED-CRAWLER.md)).

## Before pointing this at the open web

- Set a truthful `CRAWL_USER_AGENT` with a contact URL that works.
- Publish a page at that URL explaining the crawler and how to block it.
- Start at `CRAWL_INITIAL_RATE=0.5` or lower and watch `crawl_fetches_total{status_class="5xx"}`.
- Alert on `crawl_politeness_violations_total` — any value above zero, no threshold.
- Never evade a block. If a site blocks you, you are done with that site.
