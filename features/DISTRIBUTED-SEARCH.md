# Distributed Search

Scatter-gather across shards, and the tail-latency problem that dominates it.

Related: [SHARDING](SHARDING.md) · [SEARCH-ENGINE](SEARCH-ENGINE.md) · [FAILURE-HANDLING](FAILURE-HANDLING.md)

---

## Fanout topology

A flat root → 1,408 leaves fanout does not work: the root spends all its time on RPC
bookkeeping and one slow leaf stalls everything. Two levels:

```
                    root aggregator
                   /   |    |    \
              mid-0  mid-1 mid-2  mid-3        8 mid-tier parents
              / | \   / | \  ...                each covering ~16 leaves
            L0 L1 L2 ...                        128 tier-0 leaves
```

- Root fanout: 8. Mid-tier fanout: ~16.
- Mid-tier does partial merge — 16 × 1,000 candidates → top 1,000 — so the root receives
  8,000 candidates instead of 128,000.
- Adds one network hop (~1 ms) and removes an order of magnitude of merge work at the root.

---

## Tiering

Tiering turns a 1,408-shard fanout into a **128-shard fanout for 75% of traffic**.

```
root ──100%──▶ TIER 0   250 M docs · 128 shards · DRAM · ×8
                 │
                 │ spill if fewer than k results clear the
                 │ sufficiency threshold      ~25% of queries
                 ▼
               TIER 1   1.75 B docs · 256 shards · DRAM+NVMe · ×4
                 │
                 │ spill again                ~6% of queries
                 ▼
               TIER 2   8 B docs · 1,024 shards · NVMe · ×2

  ╭─ 0.1% HOLDBACK ─ bypasses the tier logic, always queries all three
  ╰─ its NDCG gap against the tiered path is the price tiering charges you
```

### The holdback is not optional

The sufficiency threshold is a **learned model**. When it fires wrongly, the user gets a
worse answer, no error is raised, no counter increments, and **no alert exists that could
fire**. It is the purest form of invisible regression: the system confidently serving results
it had the capacity to beat.

The 0.1% full-fanout holdback is the only mechanism that can detect this. Its NDCG gap must
be a reviewed metric with an owner. Without it, tiering is a cost saving of unknown quality
price — which is not a tradeoff, just a hope.

---

## Tail latency — slow is the failure mode, not dead

A dead leaf is easy: its replica takes the traffic. **A leaf running at 4× normal latency is
what destroys p99**, because a query is only as fast as its slowest of 128 shards.

With 128 shards, a per-shard p99 of 30 ms means the *query's* expected max is far worse:

```
P(no shard exceeds its p99) = 0.99^128 ≈ 0.28
```

72% of queries hit at least one p99-slow shard. **Per-shard p99 becomes per-query p50.** This
is the single most important fact about distributed search.

### The toolbox

| Technique | Mechanism | Cost |
| --- | --- | --- |
| **Deadline propagation** | Remaining budget travels with the request; a leaf receiving 4 ms declines rather than doing work nobody will wait for | Free |
| **Hedged requests** | Not answered by its p95 → send to another replica, take the first response | ~5% extra load |
| **Partial completion** | Return once 98% of shards answered + 10 ms grace | Occasional missing 2% |
| **Cancellation** | Propagate cancel on first sufficient response | Required, or hedging doubles fleet load |
| **Tied requests** | Send to two replicas, each cancels the other on start | ~10% load, better than hedging for short requests |

Missing 2% of one tier is almost always invisible in the ranked output. Waiting for it is
always visible in latency. Take the trade — but set `partial: true` in the response
([SEARCH-API](SEARCH-API.md)) and suppress caching of that result.

---

## Merge

```
receive (doc_id, score, shard_id) from each responding shard
  → verify score comparability across shards  ← see below
  → heap-merge to top N
  → collapse near-duplicate hosts (max 2 per registrable domain)
  → apply deny-list overlay                   (FAILURE-HANDLING.md)
  → L2 rerank                                 (RANKING.md)
```

### Score comparability

Shard-local IDF makes scores **not comparable across shards** ([BM25](BM25.md)). With uniform
random document assignment the error is small and shrinks with shard size — acceptable.

With **tiering it is not acceptable**: tier 0 is deliberately a biased sample of high-quality
documents, so its local corpus statistics are systematically wrong relative to tier 2's.
Broadcast global `N`, `df`, and `avgdl` with each index generation.

This is a real cost of tiering that the cost model usually forgets.

---

## Load balancing across replicas

Do **not** use round-robin. Use **least-outstanding-requests**: route to the replica with the
fewest in-flight queries. It automatically routes around a leaf that has become slow —
garbage collection, a hot neighbour, a degraded disk — without needing to detect why.

Subsetting: each mid-tier parent talks to a fixed random subset of ~6 replicas per shard
rather than all 8. Reduces connection count and improves connection reuse, at a small cost in
balance quality.

---

## Target vs Build

| | Target | Build |
| --- | --- | --- |
| Topology | root → 8 mid → 1,408 leaves | Single OpenSearch coordinator → 3 shards |
| Tiering | 3 tiers with learned spill | None — corpus fits one tier |
| Hedging | Custom, p95-triggered | OpenSearch adaptive replica selection |
| Deadlines | Propagated through every hop | `timeout` + `terminate_after` |
| Holdback | 0.1% full fanout | N/A until tiering exists |

OpenSearch's adaptive replica selection already implements a variant of
least-outstanding-requests. Use it; do not hand-roll routing at Build scale.

---

## Failure modes

| Failure | Consequence | Mitigation |
| --- | --- | --- |
| One slow leaf | Per-shard p99 becomes per-query p50 | Hedging + partial completion |
| No cancellation with hedging | Fleet load doubles | Propagate cancel on first response |
| Deadline not propagated | Leaves do work nobody waits for; wasted capacity under load | Budget travels with the request |
| Shard-local IDF across tiers | Wrong merge order — silently | Broadcast global stats per generation |
| All replicas of a shard down | Those documents silently absent | Serve with `partial: true`; alert; never fail the query |
| Round-robin routing | Traffic keeps hitting the slow replica | Least-outstanding-requests |
| Query of death | Crashes every replica it is retried on → tier down in seconds | In-flight query log + quarantine ([FAILURE-HANDLING](FAILURE-HANDLING.md)) |
| Sufficiency threshold drifts | Quality degrades with no signal | Holdback NDCG gap, monitored |
