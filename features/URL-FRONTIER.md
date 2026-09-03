# URL Frontier

Decides **what to fetch next**. It must satisfy two objectives that are in direct conflict:
fetch the most valuable URLs first, and never hit any single host too hard.

A single priority queue cannot do both. This is the central design problem.

Related: [WEB-CRAWLER](WEB-CRAWLER.md) · [URL-DE-DUPLICATION](URL-DE-DUPLICATION.md) · [DISTRIBUTED-CRAWLER](DISTRIBUTED-CRAWLER.md)

---

## Two-stage queue design (Mercator)

Separate the objectives into two queue sets, and let a heap mediate between them.

```
discovered URLs
      │
      ▼
┌──────────────┐
│ seen-URL     │  reject already-known URLs        (URL-DE-DUPLICATION.md)
│ filter       │
└──────┬───────┘
       ▼
┌──────────────┐
│ priority     │  score = f(authority, change rate, freshness debt, depth)
│ model        │
└──────┬───────┘
       ▼
╔═══════════════════╗
║ F front queues    ║  OBJECTIVE: PRIORITY
║ (1,024)           ║  one queue per priority band
║  priority 0       ║
║  priority 1       ║
║  …                ║
╚═════════╤═════════╝
          │  biased pop (high bands more often) → route by host
          ▼
╔═══════════════════╗
║ B back queues     ║  OBJECTIVE: POLITENESS
║ (3× fetch threads)║  one queue per HOST
║  host: a.com      ║
║  host: b.org      ║
║  …                ║
╚═════════╤═════════╝
          │
          ▼
┌──────────────────────┐
│ min-heap on          │  one entry per back queue
│ nextFetchTime        │  keyed ONLY on wall-clock time
└──────┬───────────────┘
       ▼
   fetcher pool  ──▶  robots + adaptive limiter  ──▶  fetch
       │
       └── refill: pull next URL for that host, recompute nextFetchTime
```

---

## Invariants

These are what make the design work. Violating any one re-couples the two objectives.

**1. A host maps to at most one back queue.**
Otherwise two fetchers race the same server and politeness is silently broken. Enforced by a
`host → back_queue_id` map maintained alongside the queues.

**2. Back queues stay non-empty while the crawl runs.**
An empty back queue idles a fetcher thread. When a back queue drains, immediately pull from
the front queues until a URL for an unassigned host is found, and rebind the queue.

**3. The heap holds only wall-clock times, never priorities.**
This is the invariant people break. If priority leaks into the heap key, a high-priority URL
can jump a host's rate limit. Priority is *fully spent* by the time a URL enters a back queue.

---

## Priority scoring

```
priority = w₁·log(authority)          static rank of the URL or its host
         + w₂·change_rate_estimate     λ from observed history
         + w₃·freshness_debt           time since last fetch ÷ expected interval
         + w₄·discovery_signal         sitemap lastmod, feed push, inbound link burst
         − w₅·depth_penalty            path depth, parameter count
         − w₆·site_budget_pressure     how much of this site's budget is already spent
```

Bands are quantised into 1,024 buckets so the front queues stay a fixed structure. Exact
scores do not need to be preserved — the ordering within a band is irrelevant at this volume.

---

## Recrawl scheduling

Model each page's change process as **Poisson with rate λ**, estimated from its own observed
history, and allocate a fixed fetch budget to maximise expected freshness.

The counterintuitive and well-established result (Cho & Garcia-Molina): the optimal
allocation is **not** proportional to λ. For pages that change faster than you could ever
revisit them, the right move is to **reduce** effort — each fetch buys almost no freshness.
Spend the budget on the middle of the distribution instead.

```
        effort
          │      ╭──────╮
          │    ╭─╯      ╰─╮
          │  ╭─╯          ╰──╮
          │╭─╯                ╰────────
          └──────────────────────────────▶ λ (change rate)
           rarely                    constantly
           changes                   changes
```

### Free signals — use all of them before spending a fetch

| Signal | Cost | Value |
| --- | --- | --- |
| `sitemap.xml` `<lastmod>` | 1 fetch per site per day | Very high — tells you what changed |
| RSS / Atom feeds | 1 fetch per feed | High for news |
| WebSub / IndexNow push | Free, publisher-initiated | Highest — near-zero-latency change notice |
| HTTP conditional GET | ~1 KB per check | High — 60% of refresh traffic ends here |

---

## The refresh/discovery tension

Refresh and discovery draw on **the same politeness-limited per-host capacity**. Every
recrawl of a known page on `example.com` is a new page on `example.com` you did not fetch.

This is zero-sum *within each host*, not a global bandwidth question — which is why the
split has to be a per-host policy:

```
host_budget = f(host_authority, host_size, host_tolerated_rate)
refresh_share = g(mean λ of known pages on this host)
discovery_share = 1 − refresh_share
```

A news site skews to refresh. A large static reference site skews to discovery until its
known-page count plateaus. Getting this wrong at the corpus level — a single global ratio —
is a common and expensive mistake.

---

## Persistence

At target scale the frontier holds 5 × 10¹¹ URLs. This does not fit in RAM.

| Layer | Contents | Medium |
| --- | --- | --- |
| Hot | Next ~6 hours of working set, all back queues, the heap | RAM |
| Warm | Front queues beyond the hot horizon | NVMe, log-structured, host-sharded |
| Cold | The long tail of known-but-unscheduled URLs | Object store, batch-loaded |

**Durability requirement is genuinely weak.** Losing an hour of frontier state costs an hour
of crawl, not correctness — the URLs are rediscoverable from the link graph. Do not pay for
strong durability here. Checkpoint every few minutes and accept the loss.

---

## Target vs Build

| | Target | Build |
| --- | --- | --- |
| Front queues | 1,024, in-process + NVMe spill | 8 Redis sorted sets by priority band |
| Back queues | 3 × fetch threads, in-process | Redis lists keyed by host |
| Heap | In-process binary heap | Redis sorted set scored by `nextFetchTime` |
| Capacity | 5 × 10¹¹ URLs | 10⁸ URLs |
| Persistence | Log-structured NVMe + object store | Redis AOF + Postgres for cold |

Redis-backed queues stop working somewhere around 10⁸–10⁹ URLs, when the working set exceeds
one machine's memory and the per-operation round-trip starts to dominate the fetch decision.
That is the migration point.

---

## Failure modes

| Failure | Consequence | Mitigation |
| --- | --- | --- |
| Back queue starvation | Fetcher threads idle while work exists | Invariant 2 — refill on drain. Alert on idle-thread ratio. |
| Priority inversion | Low-value URLs crowd out high-value ones | Biased pop must sample high bands more often; verify with a distribution assertion in tests. |
| Host explodes the frontier | One site's traps consume the whole budget | Per-site URL budget proportional to authority. Hard cap. |
| Heap and back queues disagree | A back queue is due but not in the heap, or vice versa | Single writer per back queue; reconciliation sweep on startup. |
| Lease leak | URLs stuck in-flight forever | Lease TTL with expiry sweep. Duplicate fetch is acceptable; a stuck URL is not. |
| Frontier restart | Lost scheduling state | Weak durability by design — accept it, rebuild from the link graph. |
