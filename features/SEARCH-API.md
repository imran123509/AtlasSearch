# Search API

The HTTP contract. The narrowest, most stable surface in the system — everything behind it
changes constantly, so this must not.

Related: [SEARCH-ENGINE](SEARCH-ENGINE.md) · [DISTRIBUTED-SEARCH](DISTRIBUTED-SEARCH.md) · [REDIS](REDIS.md)

---

## Endpoints

```
GET  /v1/search          the main query endpoint
GET  /v1/suggest         typeahead / autocomplete
GET  /v1/healthz         liveness  — is the process up
GET  /v1/readyz          readiness — is the index mounted and warm
GET  /v1/metrics         Prometheus scrape
```

### `GET /v1/search`

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `q` | string | required | 1–512 chars, ≤32 terms after tokenisation |
| `limit` | int | 10 | Max 50. Cost is superlinear in `limit` — see below. |
| `offset` | int | 0 | **Hard cap 1000.** See pagination. |
| `lang` | string | inferred | BCP-47 |
| `region` | string | inferred | ISO-3166-1 alpha-2 |
| `safe` | enum | `moderate` | `off` \| `moderate` \| `strict` |
| `freshness` | enum | `auto` | `auto` \| `day` \| `week` \| `year` |
| `site` | string | — | Restrict to a registrable domain |
| `explain` | bool | false | Score breakdown. Rate-limited hard; expensive. |

### Response

```json
{
  "query": {
    "raw": "block max wand",
    "corrected": null,
    "interpreted": ["block", "max", "wand"],
    "intent": "informational"
  },
  "results": [
    {
      "url": "https://example.org/papers/bmw.pdf",
      "display_url": "example.org › papers › bmw.pdf",
      "title": "Faster Top-k Retrieval with Block-Max Indexes",
      "snippet": "…the <em>block</em> upper bound lets the cursor skip…",
      "published": "2011-07-24",
      "score": 18.42,
      "doc_id": "d:8f2a91c4"
    }
  ],
  "total_estimate": 24100,
  "page": { "limit": 10, "offset": 0, "next_offset": 10 },
  "meta": {
    "took_ms": 87,
    "cache": "miss",
    "tiers_searched": [0, 1],
    "degraded": false,
    "index_generation": "gen-20260902-0600",
    "partial": false
  }
}
```

### The `meta` block is not optional

Every field there exists because someone will need it during an incident:

| Field | Why it must be in the response |
| --- | --- |
| `tiers_searched` | Tells you whether the spill logic fired. Without it, tiering bugs are invisible. |
| `degraded` | Which rung of the [degradation ladder](FAILURE-HANDLING.md) was active. |
| `partial` | Some shards did not answer. **This result set is incomplete.** |
| `index_generation` | Which generation produced this. Essential when a bad build ships. |
| `cache` | hit / miss / stale. |

`partial: true` must also suppress caching of that response — see [caching](SEARCH-ENGINE.md).

---

## Pagination: offset is capped at 1000, deliberately

Deep pagination is quadratic in a distributed search. To return results 10,000–10,010, every
one of 128 shards must produce and rank its top 10,010, and the root must merge 1.28 million
candidates — to show ten.

```
cost ∝ shards × (offset + limit)
```

Options:

1. **Hard cap at 1000** (chosen). Matches user behaviour — essentially nobody paginates past
   page 3, and the ones who do are scrapers.
2. **Search-after cursor.** Pass the last result's sort key; each shard resumes from there.
   O(limit) per page regardless of depth. Offered for API clients that genuinely need deep
   traversal.

```
GET /v1/search?q=...&search_after=eyJzIjoxOC40Miwi...
```

The cursor is opaque, signed, and bound to an index generation — it becomes invalid when the
generation flips, which is correct: resuming into a different corpus produces silently
inconsistent pages.

---

## Errors

```json
{ "error": { "code": "query_too_long", "message": "q exceeds 512 characters", "retryable": false } }
```

| Status | Code | Retryable |
| --- | --- | --- |
| 400 | `query_too_long`, `invalid_param`, `offset_too_deep` | no |
| 401 | `unauthenticated` | no |
| 429 | `rate_limited` (+ `Retry-After`) | yes |
| 503 | `overloaded` (+ `Retry-After`) | yes, with backoff |
| 504 | `upstream_timeout` | yes, once |

**Never return 500 for an overload.** 503 with `Retry-After` tells the client to back off;
500 invites an immediate retry and turns a load problem into a [retry storm](FAILURE-HANDLING.md).

An empty result set is **200 with `results: []`**, not 404. 404 means the endpoint does not
exist.

---

## Rate limiting and priority classes

Traffic is classified on arrival, and the class determines which rung of the degradation
ladder sheds it first.

| Class | Limit | Shed order |
| --- | --- | --- |
| Interactive (browser, signed session) | 60 / min / session | Last |
| Authenticated API | Per plan | Third |
| Anonymous API | 10 / min / IP | Second |
| Suspected bot | 2 / min / IP | **First** |

Token buckets in [Redis](REDIS.md); see that doc for the atomic Lua implementation.

`explain=true` gets its own much tighter bucket — it bypasses the result cache and forces a
full scoring path, so it is the cheapest way for a client to DoS the backend by accident.

---

## Query validation

Reject early and cheaply. Every one of these has been used as an attack:

```
length      ≤ 512 chars
terms       ≤ 32 after tokenisation
wildcards   leading wildcard (*foo) rejected — scans the whole term dictionary
regex       not exposed at all
nesting     boolean depth ≤ 4
site:       must parse as a registrable domain
```

Never pass user input into the backend query DSL by string concatenation. Build the query as
a typed structure and let the client library serialise it — the OpenSearch equivalent of SQL
injection is real and is usually a full-cluster read.

---

## Caching contract

- `Cache-Control: private, max-age=60` on a normal 200.
- `no-store` when `partial: true` or `degraded: true` — never persist a degraded result.
- `ETag` derived from `(normalised_query, locale, index_generation)`.
- The **index generation is part of the cache key** so a new build makes old entries
  unreachable rather than requiring a flush. See [SEARCH-ENGINE](SEARCH-ENGINE.md) for why
  that detail is load-bearing.

---

## Target vs Build

| | Target | Build |
| --- | --- | --- |
| Framework | Custom C++/Go service | FastAPI + `uvicorn` |
| Auth | Signed tokens at the edge | API key header |
| Rate limit | In-process + edge | Redis token bucket |
| Suggest | Dedicated FST-backed service | OpenSearch `completion` suggester |
| p99 | 400 ms | 800 ms |

---

## Failure modes

| Failure | Consequence | Mitigation |
| --- | --- | --- |
| Deep pagination allowed | Trivial DoS, quadratic cost | Cap offset; offer `search_after` |
| Degraded results cached | One bad minute poisons the cache for an hour | `no-store` on `degraded`/`partial` |
| 500 on overload | Clients retry immediately → retry storm | 503 + `Retry-After` |
| Unbounded query length | Backend memory blowup | Validate before dispatch |
| Leading wildcard permitted | Full term-dictionary scan per query | Reject |
| Query DSL string-built | Injection → full cluster read | Typed query construction only |
| Cursor survives generation flip | Silently inconsistent pages | Bind cursor to generation; invalidate on flip |
