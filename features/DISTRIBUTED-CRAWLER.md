# Distributed Crawler

Running the crawler across many machines **without breaking politeness**. That constraint is
what makes this hard — otherwise it is embarrassingly parallel.

Related: [WEB-CRAWLER](WEB-CRAWLER.md) · [URL-FRONTIER](URL-FRONTIER.md) · [KAFKA](KAFKA.md)

---

## The core problem

Politeness is a **per-host** constraint. If two machines fetch `example.com` simultaneously,
you have violated it — regardless of how carefully each machine limits itself.

Two ways to solve it:

| Approach | Mechanism | Cost |
| --- | --- | --- |
| **Shared rate limiter** | Every fetch checks Redis | A network round trip per fetch decision. Dies above ~10 K/s. |
| **Host affinity** ✅ | Each host is owned by exactly one worker; limiter is in-process | Requires stable ownership and careful handoff |

Host affinity is the answer at scale. Shared limiters are the answer at Build scale, and the
migration between them is a real event, not a config change.

---

## Host affinity via partition ownership

The trick is to get partition assignment to *do* the ownership for you:

```
urls.scheduled  keyed by registrable_domain, 256 partitions      (KAFKA.md)
        │
        └─ Kafka assigns each partition to exactly one consumer in the group
                │
                └─ therefore each domain is owned by exactly one worker
                        │
                        └─ therefore the rate limiter can live in that worker's memory
```

No distributed coordination, no Redis on the fetch path, no lock. **Politeness becomes a
consequence of the partitioning scheme.** This is the single best reason to key `urls.*` by
domain.

### Consistent hashing as the alternative

If not using Kafka group assignment:

```
worker = consistent_hash_ring.get(registrable_domain)
```

With virtual nodes (~150 per worker) for balance. Adding or removing a worker moves only
`1/n` of hosts. Requires a membership service (etcd, Consul) and careful handling of the
window where two workers disagree about ownership.

Kafka's group protocol already solves membership, failure detection, and rebalance. Use it.

---

## The rebalance window

When a worker dies, Kafka reassigns its partitions. For a few seconds, the old owner may
still be finishing in-flight fetches while the new owner starts.

```
t=0    worker A owns partition 7 (example.com)
t=10   worker A stops heartbeating
t=15   rebalance: worker B takes partition 7
t=15   worker A is still mid-fetch on example.com  ← overlap
```

Mitigations:

- **Cooperative sticky assignor** — only the affected partitions move, and the old owner gets
  `onPartitionsRevoked` to drain first.
- **Drain on revoke**: finish in-flight, do not start new fetches, then release.
- **Conservative start**: a new owner begins at half the host's known rate and ramps up, so a
  brief overlap does not double the request rate.
- **Session timeout** shorter than the politeness window, so overlap is bounded.

Brief overlap is acceptable. Sustained overlap is not — alert if a partition has two claimants
for more than a few seconds.

---

## Leases

Within a worker, URLs are leased so a crash does not lose them permanently:

```
lease:{url_hash} = worker_id, TTL 300s        (REDIS.md)

acquire → fetch → release
crash   → TTL expires → URL returns to the frontier
```

**At-least-once, deliberately.** A duplicate fetch costs one page load. Exactly-once would
cost a distributed transaction on every URL, and the failure mode it prevents is one wasted
HTTP request. Not worth it.

Content de-duplication downstream makes duplicate fetches harmless to the index
([CONTENT-DE-DUPLICATION](CONTENT-DE-DUPLICATION.md)).

---

## Worker topology

```
┌─────────────────────────────────────────────────────┐
│  frontier workers    ×  32                          │
│  consume urls.discovered → dedup → prioritise       │
│  produce urls.scheduled                             │
└─────────────────────────────────────────────────────┘
                        │
┌─────────────────────────────────────────────────────┐
│  fetch workers       ×  256                         │
│  own partitions of urls.scheduled by domain         │
│  in-process rate limiter + robots cache             │
│  produce pages.fetched (blob pointer)               │
└─────────────────────────────────────────────────────┘
                        │
┌─────────────────────────────────────────────────────┐
│  render workers      ×  40    (memory-bound, not CPU)│
│  headless browsers, ~700 concurrent                 │
└─────────────────────────────────────────────────────┘
                        │
┌─────────────────────────────────────────────────────┐
│  parse workers       ×  128                         │
│  consume pages.fetched → produce pages.parsed       │
└─────────────────────────────────────────────────────┘
```

Render workers are a **separate pool** because they are memory-bound (~300 MB each) while
fetch workers are I/O-bound and parse workers are CPU-bound. Mixing them means provisioning
every host for the worst dimension of all three.

---

## Politeness state on rebalance

The in-process rate limiter's state is lost when a partition moves. That is *mostly* fine —
the limiter re-learns within a few requests — but for high-value hosts it means a burst.

Checkpoint the important part:

```
every 30 s, write per-host {rate, last_fetch_ts, health} to Redis
on partition acquire, load the checkpoint for those hosts
```

Redis is a **cache of politeness state, not the enforcement point**. Enforcement stays
in-process. Losing the checkpoint costs a brief ramp-up, not a violation, because of the
conservative-start rule above.

---

## Target vs Build

| | Target | Build |
| --- | --- | --- |
| Fetch workers | 256 | 1–4 |
| Ownership | Kafka partition assignment | Single process, or Redis limiter |
| Rate limiter | In-process, checkpointed | Redis Lua ([REDIS](REDIS.md)) |
| Render pool | 40 hosts, separate | Same process, allow-listed |
| Rate | 10,200 fetches/s | 20–50 fetches/s |

Below ~1 K fetches/s, a Redis-based shared limiter is simpler and correct. Migrate to host
affinity when the Redis round trip starts showing up in the fetch loop's profile.

---

## Failure modes

| Failure | Consequence | Mitigation |
| --- | --- | --- |
| Two workers own one host | Politeness violated → you get blocked or sued | Partition-based ownership; alert on dual claim |
| Rebalance storm | No fetching while the group churns | Cooperative sticky assignor; session timeout tuned above GC pauses |
| Worker crash | In-flight URLs stuck | Lease TTL returns them |
| Shared limiter at high rate | Fetch loop bottlenecks on Redis | Move in-process |
| Render pool mixed with fetch pool | Every host provisioned for 300 MB × concurrency | Separate pools |
| New owner starts at full rate | Burst against a host right after rebalance | Conservative start + ramp |
| Politeness state lost | Brief over-crawl | Checkpoint to Redis; conservative start covers the gap |
| Parse lag ignored | Kafka retention expires, pages lost silently | Lag-based backpressure to crawl rate ([KAFKA](KAFKA.md)) |
