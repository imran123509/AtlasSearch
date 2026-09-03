# Distributed Indexing

Turning 880 M documents/day into an immutable, versioned [index generation](INVERTED-INDEX.md).

This is a large batch job with two global shuffles, and it is the scariest operation in the
system — a bad generation is bad in **every replica**, so replication provides zero protection.

Related: [INVERTED-INDEX](INVERTED-INDEX.md) · [PAGERANK](PAGERANK.md) · [CONTENT-DE-DUPLICATION](CONTENT-DE-DUPLICATION.md) · [KAFKA](KAFKA.md)

---

## Pipeline

```
pages.parsed
    │
    ├─▶ [shuffle by target_domain] ─▶ ANCHOR AGGREGATION
    │       anchor text discovered on SOURCE pages must reach TARGET pages
    │
    ├─▶ [shuffle by simhash band]  ─▶ NEAR-DUP CLUSTERING ─▶ canonical election
    │
    ├─▶ [graph job, host-partitioned] ─▶ PAGERANK  (weekly, warm-started)
    │
    ▼
  JOIN on doc_id  →  document + anchors + authority + canonical flag
    │
    ├─▶ tier assignment      f(pagerank, quality, traffic, freshness)
    ├─▶ feature payload      ~1 KB of L1 ranking features
    ├─▶ embedding + PQ       one vector per document
    │
    ▼
  [shuffle by (tier, shard_id)]  →  SEGMENT BUILD
    sort by (term, static_rank, doc_id)
    encode postings, positions, block maxima
    write segment files + checksums
    │
    ▼
  GENERATION MANIFEST  →  validate  →  publish
```

---

## The two global shuffles

Both are unavoidable, and both are where the job's cost lives.

### 1. Anchor aggregation

Anchor text is discovered on **source** pages and must be delivered to **target** pages. It is
frequently the best description of a document, and it is the only way to index images, PDFs,
and pages whose own text is useless.

```
map:     for each link (source, target, anchor, rel): emit (target_domain, ...)
shuffle: 5 × 10¹¹ edges
reduce:  group by target doc_id → anchor field for that document
```

Cap and diversify during the reduce:

```
max 1,000 anchors per target
cap contribution per source registrable domain     ← or Google-bombing works
require domain diversity for high anchor weight
drop rel=nofollow / ugc / sponsored from authority, keep for discovery
```

### 2. Near-duplicate clustering

Needs to compare documents living on different crawl shards, so it must be global. Shuffle by
SimHash band value; cluster within band; elect a canonical.
See [CONTENT-DE-DUPLICATION](CONTENT-DE-DUPLICATION.md).

Only canonicals are indexed. Aliases become redirects in the document store.

---

## Segment build

```
input:  documents assigned to (tier, shard)
sort:   by (term, static_rank, doc_id)
```

**Sorting by `static_rank` within each term is what makes block-max pruning work**
([INVERTED-INDEX](INVERTED-INDEX.md)). It is also why changing the authority formula requires
a full rebuild — the ordering is baked in.

```
for each term:
    for each block of 128 documents:
        compute maxScore over the block          ← must be an upper bound, or results are wrong
        delta-encode docIDs, bitpack freqs
        write positions to the separate stream
    write dictionary entry (df, cf, offset, block count)
```

Shuffle volume: 880 M docs × 600 postings ≈ **5.3 × 10¹¹ postings/day**, ~5.3 TB pre-compression.
Roughly 500 machines for a few hours.

---

## Incremental vs full

| Cadence | Scope | Duration | Trigger |
| --- | --- | --- | --- |
| Real-time | In-memory layer, high-λ pages | seconds | Continuous |
| Hourly | New + recrawled docs → new segments | ~40 min | Schedule |
| Daily | Merge, apply tombstones, refresh static rank overlay | ~4 h | Schedule |
| **Full rebuild** | Everything: re-sort, re-tier, re-score | ~18 h | Schema / analyzer / authority change |

### Full rebuilds are the scariest operation

An 18-hour job that fails at hour 17 is unacceptable. Requirements:

- **Checkpoint per stage.** Anchor aggregation, clustering, PageRank, and segment build each
  persist their output. A failure resumes from the last checkpoint, not from zero.
- **Shard-level independence.** Segment build for shard 42 must not depend on shard 43.
  A single failed shard reruns alone.
- **Design to never need one.** Prefer segment-level rebuild. Every mechanism that avoids a
  full rebuild — the authority overlay, the deny-list, tombstones — exists partly for this
  reason.

---

## Generation manifest and validation

```json
{
  "generation": "gen-20260902-0600",
  "created_at": "2026-09-02T09:41:22Z",
  "doc_count": 10041882931,
  "analyzer_version": "v7",
  "corpus_stats": { "N": 10041882931, "avgdl": 1187.4 },
  "tiers": [ {"tier": 0, "shards": 128, "docs": 250118432}, ... ],
  "shards": [ {"id": "t0.s000", "blob": "s3://...", "sha256": "...", "bytes": 33214...} ],
  "parent": "gen-20260901-0600"
}
```

`analyzer_version` and `corpus_stats` are in the manifest because both must match at query
time — a mismatched analyzer is a silent total quality failure
([TOKENIZATION](TOKENIZATION.md)), and stale `avgdl`/`N` shift every score
([BM25](BM25.md)).

### The rollout gate

Replication does **not** protect against a bad generation. The gate is the protection:

```
build
  → checksum every shard file
  → structural validation (dictionary/posting consistency, block-max monotonicity)
  → 2 h of MIRRORED SHADOW TRAFFIC scored on NDCG, latency, crash rate
  → single-shard canary
  → progressive rollout, automatic halt on guardrail regression
  → generation N−1 stays mounted throughout
```

Rollback is a pointer flip. Keep N−1 hot until N has been serving cleanly for a full day.

---

## Target vs Build

| | Target | Build |
| --- | --- | --- |
| Engine | Custom MapReduce / Spark | Spark local, or a single Python process |
| Throughput | 880 M docs/day | 100 K docs/day |
| Shuffle | 5.3 TB/day | ~1 GB/day |
| PageRank | Pregel, host-partitioned | SciPy sparse |
| Segment build | Custom encoder | OpenSearch bulk index ([OPENSEARCH](OPENSEARCH.md)) |
| Publication | Generation artifact + manifest | Index alias swap |

At Build scale the alias swap is the whole mechanism:

```
create index docs-v7 → bulk load → validate → POST _aliases {remove: docs-v6, add: docs-v7}
```

Atomic, and `docs-v6` stays for rollback. Same idea, one line.

---

## Failure modes

| Failure | Consequence | Mitigation |
| --- | --- | --- |
| Bad generation shipped | Wrong results in **every replica** | Shadow traffic + canary + progressive rollout + N−1 hot |
| Wrong block-max bounds | Results silently wrong (pruned documents that should have won) | Assert bounds are upper bounds during build; property test |
| Analyzer version drift | Index unreachable by queries | Version in manifest; refuse to serve on mismatch |
| Stale `avgdl` / `N` | Scores drift between generations | Recompute per generation, ship in the manifest |
| 18 h job fails at hour 17 | A day lost | Per-stage checkpoints, shard-level independence |
| Anchor text uncapped | Off-page attackers rank arbitrary documents | Cap per source domain, require diversity |
| Shuffle skew (one huge domain) | One reducer runs for hours | Salt keys for high-volume domains |
| Merge storm after publish | I/O saturation, latency spike | Rate-limit merges; stagger generation flips by shard group |
