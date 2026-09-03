# Failure Handling

At 22,000 hosts something is always broken. The goal is not to prevent failure but to make
the common failures boring and the correlated ones survivable.

Related: [DISTRIBUTED-SEARCH](DISTRIBUTED-SEARCH.md) · [MONITORING](MONITORING.md) · [DISTRIBUTED-INDEXING](DISTRIBUTED-INDEXING.md)

---

## Slow is the failure mode, not dead

A dead leaf is easy — its replica takes the traffic. **A leaf running at 4× normal latency is
what destroys p99**, because a query is only as fast as its slowest of 128 shards.

```
P(no shard exceeds its p99) = 0.99^128 ≈ 0.28
→ 72% of queries hit at least one p99-slow shard
→ per-shard p99 becomes per-query p50
```

| Technique | Mechanism | Cost |
| --- | --- | --- |
| Deadline propagation | Remaining budget travels with the request; a leaf with 4 ms left declines | Free |
| Hedged requests | No answer by p95 → send to another replica, take the first | ~5% load |
| Partial completion | Return at 98% of shards + 10 ms grace | Occasional 2% loss |
| Cancellation | Cancel on first sufficient response | Required, or hedging doubles load |

---

## The degradation ladder

Overload behaviour is a **designed artifact with named rungs**, not an emergent property of
timeouts. Each rung triggers automatically on backend utilisation and is individually
testable — and each is exercised in a game day, because an untested rung is a hypothesis.

| Rung | Trigger | Action | Quality cost |
| --- | --- | --- | --- |
| **0** | — | Full path | — |
| **1** | > 70% | Drop L3 cross-encoder for tail queries, keep it for head | Small NDCG loss where least measured |
| **2** | > 80% | Cap spill at tier 1; reduce *k* from 1,000 → 400 | Recall loss on rare queries |
| **3** | > 88% | Tier 0 only; snippets from cache or leading-text fallback | Visibly worse snippets, thin tail results |
| **4** | > 94% | Serve cache entries past TTL; shed by priority class | Stale results; API traffic degraded first |
| **5** | > 98% | 503 + `Retry-After` for lowest-priority classes | Outage for a defined slice, not for everyone |

Reducing *k* works because retrieval cost is **superlinear in *k*** — θ rises faster with a
smaller heap, so block-max pruning bites harder ([INVERTED-INDEX](INVERTED-INDEX.md)). Rung 2
is cheaper than it looks.

The current rung is reported in every response (`meta.degraded`) so it is visible in logs and
to clients ([SEARCH-API](SEARCH-API.md)).

---

## Correlated failures — the ones that matter

Independent failures are handled by replication. These are not.

### A bad index generation is bad in every replica

Replication provides **zero** protection here, which makes this the most dangerous failure in
the system. The gate is the protection:

```
build
  → checksum every shard file
  → structural validation (block-max bounds are upper bounds, dictionary consistency)
  → 2 h mirrored SHADOW TRAFFIC scored on NDCG, latency, crash rate
  → single-shard canary
  → progressive rollout, automatic halt on guardrail regression
  → generation N−1 stays mounted throughout
```

Rollback is a pointer flip. Keep N−1 hot until N has served cleanly for a full day.

### Query of death

A malformed query that segfaults a leaf will segfault **every replica it is retried against**,
taking down a whole tier in seconds. Retries turn one crash into a cluster outage.

```
before dispatch:  append (query_id, query) to a local in-flight log
on process start: any query in the log from before the crash → quarantine list
                  quarantined queries are served from a degraded path
```

This is the single highest-value piece of defensive code in the serving stack, and it is
usually written after the first outage rather than before.

### Retry storms

Retrying at three levels turns one failure into 27 requests.

```
retry budget:     ≤ 10% of request volume, enforced client-side
circuit breakers: per downstream, half-open probing
backoff:          exponential with full jitter
retry levels:     exactly ONE level of the call tree may retry
```

### Cold cache

Losing the results cache is a **2.2× step in backend load**, which exceeds capacity.

Mitigations, in order:

1. **Never flush globally.** The index generation is in the cache key, so a flip makes old
   entries unreachable and they age out via LRU instead of vanishing at once
   ([REDIS](REDIS.md)).
2. **Stagger generation flips** across shard groups.
3. **Pre-warm** from the static head-query list before taking traffic.
4. **Accept that rungs 1–3 are the real plan.** You cannot afford 2.2× headroom.

---

## Crawl-side failures

| Failure | Consequence | Handling |
| --- | --- | --- |
| Fetcher dies | In-flight URLs stuck | Lease TTL expires, URLs return to the frontier |
| Duplicate fetch | One wasted page load | Accepted. At-least-once is deliberate — exactly-once costs a distributed transaction to prevent one HTTP request. |
| Frontier state lost | An hour of crawl scheduling | Weak durability by design; rebuild from the link graph |
| Parse lag grows | Kafka retention expires → **silent page loss** | Lag-based backpressure to crawl rate ([KAFKA](KAFKA.md)) |
| Host starts tarpitting | Fetcher threads blocked | Hard per-request deadline |
| Politeness violated | Blocked, or legal exposure | Alert on any occurrence. Not a threshold — any. |

---

## The legal deletion path

Right-to-be-forgotten, DMCA, and court-ordered removals must take effect in **minutes** and be
auditable. Immutable segments cannot satisfy that.

```
deny-list overlay
  densely-encoded bitmap of suppressed doc_ids
  replicated to every leaf within minutes
  applied as a post-retrieval filter
  actual removal happens at the next segment merge
```

Worth being explicit: this mechanism exists for **legal reasons first and correctness
second**. It is a compliance surface, so it needs an audit log the ordinary serving path does
not have — who requested, who approved, when applied, when confirmed on every replica.

The same overlay channel carries spam demotions, because adversaries adapt in hours while the
pipeline moves in days. That is the honest reason the clean two-plane separation
([README](../README.md)) is broken on purpose.

---

## Game days

An untested rung is a hypothesis. Quarterly, in production, during business hours:

| Exercise | Validates |
| --- | --- |
| Kill 30% of leaf replicas | Partial completion, `partial: true`, no user-visible errors |
| Inject 500 ms latency into one leaf | Hedging, deadline propagation |
| Flush the results cache | Rung 1–3 activation, backend survives the step |
| Publish a deliberately bad generation to canary | Shadow gate halts it before rollout |
| Kill the metadata store | Serving continues on cached shard map |
| Send a known query-of-death | Quarantine works, tier survives |
| Drain a node during peak | PDBs hold ([KUBERNETES](KUBERNETES.md)) |

---

## Incident checklist

```
1. What is the user-visible symptom?         (not "CPU is high")
2. meta.degraded rung, meta.partial rate, meta.index_generation
3. Did a generation flip recently?           → roll back to N−1, it is a pointer flip
4. Did a deploy happen recently?             → roll back
5. Kafka lag by topic                        → is the pipeline the cause or the victim?
6. Which stage in the latency histogram?     → almost always snippets or a straggler leaf
7. Stop the bleeding first, diagnose second. Rungs exist to be used manually.
```

---

## Failure mode summary

| Class | Example | Protection |
| --- | --- | --- |
| Independent | One host dies | Replication |
| **Correlated** | Bad index generation | Shadow gate + canary + N−1 hot. **Replication does not help.** |
| Latency | One slow leaf | Hedging, deadlines, partial completion |
| Overload | Traffic spike, cache flush | Degradation ladder |
| Poison input | Query of death | In-flight log + quarantine |
| Amplification | Retry storm | Budgets, breakers, one retry level |
| Silent quality | Tier threshold drift, boilerplate remover eating content | Holdback + corpus-health metrics ([MONITORING](MONITORING.md)) |

The last row is the one that has no error signal at all. Everything else announces itself.
