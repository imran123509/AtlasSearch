# Sharding

How 10¹⁰ documents are split across machines. The choice determines everything downstream.

Related: [INVERTED-INDEX](INVERTED-INDEX.md) · [DISTRIBUTED-SEARCH](DISTRIBUTED-SEARCH.md) · [BM25](BM25.md)

---

## Document vs term partitioning

```
DOCUMENT-PARTITIONED  (chosen)          TERM-PARTITIONED
each shard: all terms, slice of docs    each shard: whole lists, slice of terms

      coordinator                             coordinator
     ╱   │   │   ╲                           ╱     │     ╲
   S0   S1  Sn-1  Sn                  shard("black") shard("merger") shard("hole")
    ↑    ↑   ↑    ↑                    4.1 B postings
    top-1000 scored ids                      │
    ≈ 40 KB per shard                        └─ ships posting list ≈ 900 MB
                                                to do the intersection

 every shard touched, but each does      only 3 shards touched, but the data
 bounded work and returns a fixed         has to move, and the shard owning
 payload. load uniform by construction.   common terms is hit on every query.

 COST: n × per-shard latency floor        COST: 10⁴× load skew (Zipf)
       paid on every query                      + multi-GB network transfers
 WINS: no skew, trivial updates,          WINS: far less aggregate CPU on
       trivial replication                       short queries
```

### The honest position

At 3.1 terms per query, term partitioning touches 3 shards where document partitioning
touches 128. **In pure aggregate work, document partitioning is dramatically worse**, and it
is chosen anyway for:

1. **Zipf load skew.** The shard holding `the` is ~10⁴× hotter than the shard holding
   `merger`. No balancing scheme fixes this, because it is a property of language.
2. **Network transfer.** Intersecting requires co-locating postings. Shipping a 900 MB list
   per query is absurd at any fanout.
3. **Update complexity.** Adding a document touches ~600 term shards under term partitioning
   and exactly 1 under document partitioning.
4. **Failure blast radius.** Losing a term shard makes some queries *impossible*. Losing a
   document shard makes all queries slightly *less complete*. The second is far better.

That aggregate CPU waste is real and large. It buys operational simplicity and predictable
tail latency. Revisit if the query mix shifts hard toward 1–2 term queries, or if network
fabrics make a 900 MB transfer unremarkable.

Hybrid schemes — term-partition the head terms, document-partition the rest — have been tried
repeatedly and add substantial complexity for modest gain.

---

## Tier layout

| Tier | Docs | Index | Shards | Shard size | Medium | Repl. | Hosts | Sized by |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **0** | 250 M | 4 TB | 128 | 31 GB | DRAM | ×8 | 1,024 | **QPS** |
| **1** | 1.75 B | 14 TB | 256 | 55 GB | DRAM+NVMe | ×4 | 1,024 | **QPS** |
| **2** | 8 B | 50 TB | 1,024 | 49 GB | NVMe | ×2 | ~1,400 | **CPU + min availability** |

### Where tier-0 sizing comes from

```
150 K peak QPS
  − 55% results-cache hit rate
  = 67 K QPS reaching retrieval
  × 128 shards
  = 8.6 M leaf RPCs/s

tier-0 leaf RPC ≈ 3 ms CPU (in-RAM postings, block-max, L1 rerank over ~200)
64-vCPU host at 60% utilisation ≈ 12 K RPC/s

8.6 M ÷ 12 K ≈ 720 hosts → round to ×8 replication over 128 shards = 1,024 hosts
```

### The observation worth keeping

Tier-0 replication is set by **throughput** (×8 to serve the QPS). Tier-2 replication is set
by **availability** (×2 as a floor, because losing both replicas means those documents
vanish).

These are different engineering problems wearing the same word. Confusing them is how fleets
end up simultaneously over-provisioned for storage and under-provisioned for load.

Note also that shard count and host count are **decoupled by packing** — tier 2 packs ~1.5
shard-replicas per host, because its constraint is CPU, not memory.

---

## Assignment

### Documents to shards — random, not semantic

```
shard_id = hash(doc_id) mod shard_count      within a tier
```

Random assignment gives uniform load and makes shard-local corpus statistics a good estimator
of global ones ([BM25](BM25.md)).

**Do not shard by topic, language, or domain.** It is tempting — "route language queries to
the language shard" — and it fails:

- Load follows the popularity distribution of topics, which is Zipfian. Skew returns.
- Cross-topic queries need every shard anyway.
- Multilingual documents have no home.
- The routing decision becomes a quality-critical classifier, with the same invisible-failure
  problem as the tier sufficiency threshold.

### Documents to tiers — semantic, and this one is fine

```
tier = f(pagerank, quality_score, traffic_history, freshness)
```

Recomputed each full rebuild. A document moving between tiers is normal. Tier assignment is a
quality decision and is measured by the [holdback](DISTRIBUTED-SEARCH.md).

### Replicas to hosts — constraint-solved, not hashed

Replicas of a shard must not share a rack, a power domain, or a switch. Consistent hashing
would happily place three replicas behind one top-of-rack switch. This is a placement solver,
not a hash function.

---

## Rebalancing

Because the index is an **immutable artifact**, rebalancing is far easier than in a database:
there is no live data migration, no dual-write, no cutover. Change the shard map in the next
generation and let hosts mount different files.

```
gen N   : 128 shards, host H serves shard 42
gen N+1 : 192 shards, host H serves shard 71
          → H downloads shard 71 while still serving 42 from gen N
          → flip when the download completes and validates
          → drop gen N when all hosts have flipped
```

Resharding costs one generation's worth of extra disk and a slow background copy. This is the
main payoff of the read-only serving plane.

---

## Target vs Build

| | Target | Build |
| --- | --- | --- |
| Partitioning | Document, 3 tiers, 1,408 shards | Document, 1 tier, 3 shards |
| Replication | ×8 / ×4 / ×2 by tier | ×1 (dev) or ×2 (staging) |
| Placement | Rack-aware constraint solver | OpenSearch shard allocation awareness |
| Rebalance | Generation-based file swap | `_cluster/reroute`, live |
| Shard size | 31–55 GB | 10–20 GB |

Keep shards in the **10–50 GB** range at any scale. Smaller wastes per-shard overhead; larger
makes recovery, rebalancing, and merges painfully slow.

---

## Failure modes

| Failure | Consequence | Mitigation |
| --- | --- | --- |
| Semantic sharding | Zipf skew returns; hot shard | Hash-based random assignment |
| Replicas co-located | One rack loss takes a shard offline | Rack-aware placement solver |
| Too many shards | Fanout overhead dominates; root merge cost explodes | Tier; keep shards 10–50 GB |
| Too few shards | Shard too large to recover or merge in reasonable time | Same rule |
| Shard-local IDF across non-uniform tiers | Wrong merge order, silently | Broadcast global stats per generation |
| Tier assignment drifts | Good documents stranded in tier 2 | Holdback NDCG gap; re-evaluate each rebuild |
| Replication factor set by storage intuition | Over-provisioned for disk, under for QPS | Size tier 0 by throughput, tier 2 by availability |
