# Search Engine — end-to-end query path

How a query becomes a results page, and where the 120 ms goes.

Related: [SEARCH-API](SEARCH-API.md) · [DISTRIBUTED-SEARCH](DISTRIBUTED-SEARCH.md) · [RANKING](RANKING.md) · [FAILURE-HANDLING](FAILURE-HANDLING.md)

---

## The path

```
 1. edge            TLS, geo-route, bot classification, priority class
 2. validate        length, term count, wildcards          (SEARCH-API.md)
 3. ┌─ results cache lookup ─────────┐   55% of queries end here
    └─ query understanding ──────────┘   run in PARALLEL, not sequentially
 4. retrieval       fanout to tier 0 leaves                (DISTRIBUTED-SEARCH.md)
 5. spill?          tier 1 → tier 2 if results insufficient
 6. merge           dedup by host, L2 rerank               (RANKING.md)
 7. L3 rerank       cross-encoder over top 60
 8. snippets        second fanout to doc servers  ← the p99
 9. blend           verticals, knowledge panel
10. assemble        render, log, cache
```

**Step 3 runs in parallel.** Query understanding costs ~6 ms; doing it before the cache
lookup adds 6 ms to the 55% of queries that were going to hit the cache anyway. Fire both,
cancel the understanding work on a cache hit.

---

## Latency budget

Server-side, tier-0-only path:

| Stage | p50 | p99 |
| --- | --- | --- |
| Edge, TLS, bot classification | 2 | 8 |
| Query understanding | 6 | 18 |
| Results-cache lookup | 1 | 4 |
| Fanout → 128 leaves | 14 | 45 |
| Merge, host-dedup, L2 rerank | 8 | 22 |
| L3 cross-encoder | 18 | 40 |
| **Snippet generation** | **34** | **95** |
| Vertical blending, assembly | 9 | 24 |
| **Total** | **92** | **256** |
| + tier-1 spill (25% of queries) | +25 | +70 |
| + tier-2 spill (6% of queries) | +55 | +180 |

Deep-spill p99 ≈ **506 ms**, over the 400 ms SLO. That is where the budget actually lives or
dies, and it is why `tiers_searched` is in the API response.

---

## Snippets are the p99, and most designs omit them

The largest single stage is not retrieval or ranking. It is fetching ten documents' text and
summarising them against the query.

It requires:

- A **second full-corpus store** at serving time — the 20 TB of document text in
  [STORAGE](STORAGE.md). Any capacity plan that sizes "the index" and forgets this is short
  by roughly a third.
- A **second fanout**, keyed by docID rather than term, with its own tail behaviour.
- It happens **after** ranking, so it cannot be overlapped with retrieval.

### Generation

```
for each of the top 10 documents:
    fetch stored text from the doc server        (sharded by docID)
    locate best-matching passage:
        score each sentence window by query term
        coverage × proximity × position prior
    trim to ~160 chars on a word boundary
    highlight query terms (including stemmed variants)
```

Cache aggressively, keyed on `(doc_id, query_term_hash)`. The hit rate is high because the
same document is snippeted for many related queries — much higher than the results cache.

**Fallback when the doc server is slow:** use the stored `meta_description` or the first 160
characters. Visibly worse, but a fast bad snippet beats a slow good one, and this is
[rung 3](FAILURE-HANDLING.md) of the degradation ladder.

---

## Caching

Queries follow a power law, so the results cache is not an optimisation — it is **load-bearing
capacity**. Fleet sizing assumes a 55% hit rate; at 0% the same traffic needs 2.2× the hosts.

| Cache | Key | Hit rate | TTL |
| --- | --- | --- | --- |
| Results | `(normalised_q, locale, safe, generation)` | ~55% | 5 min – 24 h by intent |
| Snippet | `(doc_id, query_term_hash)` | ~75% | Until generation flip |
| Doc text | `doc_id` | ~60% | Until generation flip |
| Intersection | `(term_a, term_b)` at the leaf | ~30% | In-process LRU |

### Admission policy — cache on second sight

Roughly **15% of daily queries have never been seen before**. Caching them on first sight
fills the cache with entries that will never be read. Track a lightweight "seen once" sketch
and only admit on the second occurrence.

Combine with a **static/dynamic split**: precompute the head from log analysis, run LRU for
the rest. Beats pure LRU meaningfully.

### TTL by intent

```
navigational   ("facebook login")        24 h   — stable
informational  ("how does tls work")      6 h
fresh-intent   ("earthquake")             5 min — freshness classifier says so
trending                                  60 s
```

---

## The cache / freshness / personalisation trilemma

You can have any two.

```
PERSONALISE BEFORE THE CACHE          PERSONALISE AFTER THE CACHE   ← chosen
request → personalise → cache         request → cache → backend → reorder/filter
                ↑                                    ↑
        key: q + userID                      key: q + locale + generation
        hit rate ≈ 0                         hit rate 55%
        backend sees 100% of traffic         personalisation can only reorder
        fleet must be 2.2× larger            what the cached set contains
```

Personalising after the cache caps personalisation at **reordering or removing** what the
cached set already contains. It can never retrieve a document for one user that it did not
for others.

That is right for cost, and it **forecloses recall-level personalisation** entirely. If a
competitor's differentiator turns out to be exactly that, this architecture cannot follow
without re-founding the caching layer. Take the trade — but as a stated strategic bet with an
owner, not as an implementation detail.

### The generation-keyed cache detail

Putting `index_generation` in the cache key means entries from generation N−1 become
**unreachable** rather than being flushed. A generation flip ages the cache out gradually
instead of dropping the hit rate to zero in one step.

That step would be fatal: 55% → 0% is a **2.2× step in backend load**, which exceeds
capacity. You cannot afford 2.2× headroom, so this detail is not a nicety — it is the reason
generation flips are survivable. See [FAILURE-HANDLING](FAILURE-HANDLING.md).

---

## Target vs Build

| | Target | Build |
| --- | --- | --- |
| Cache | Sharded in-memory + edge CDN | Redis ([REDIS](REDIS.md)) |
| Snippets | Dedicated doc-server fleet | OpenSearch `highlight` (stored fields) |
| L3 | Accelerator-batched cross-encoder | CPU cross-encoder, top 20 |
| Verticals | Federated with triggering models | None initially |
| p50 / p99 | 92 / 256 ms | ~200 / 800 ms |

---

## Failure modes

| Failure | Consequence | Mitigation |
| --- | --- | --- |
| Cache flushed globally | 2.2× backend load step → cascading failure | Generation in the key; never flush; stagger flips |
| Degraded results cached | Bad minute poisons the cache for hours | `no-store` on `degraded`/`partial` |
| Snippet fanout slow | p99 blows the SLO | Deadline; fall back to `meta_description` |
| Query understanding serialised before cache | +6 ms on 55% of traffic for nothing | Run in parallel, cancel on hit |
| Singleton queries admitted to cache | Cache fills with never-read entries | Cache on second sight |
| Personalisation moved before the cache | Hit rate → 0, fleet undersized by 2.2× | Keep it after. This is a load-bearing constraint. |
