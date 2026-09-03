# Inverted Index

Maps terms to the documents containing them. It is the core data structure, and its layout
determines whether a query costs 5 ms or 5 seconds.

Related: [TOKENIZATION](TOKENIZATION.md) · [BM25](BM25.md) · [SHARDING](SHARDING.md) · [DISTRIBUTED-INDEXING](DISTRIBUTED-INDEXING.md)

---

## Sizing at 10¹⁰ documents

From 600 unique terms and 1,200 tokens per document:

| Structure | Count | Encoded | Encoding |
| --- | --- | --- | --- |
| Postings (docID + freq) | 6 × 10¹² | **8 TB** | d-gaps + SIMD-BP128, ~1.3 B/posting |
| Positions | 1.2 × 10¹³ | **11 TB** | delta + varint, separate stream |
| Anchor-text index | 2 × 10¹¹ | 1.5 TB | fielded, own posting lists |
| Phrase / bigram index | — | 6 TB | head bigrams only |
| Term dictionary | 2 × 10⁹ terms | 0.4 TB | FST, front-coded |
| Forward index + attributes | 10¹⁰ docs | 5 TB | static rank, lang, date, filter bitmaps |
| Per-doc ML feature payloads | 10¹⁰ docs | 10 TB | ~1 KB of L1 ranking features |
| Dense vectors | 10¹⁰ × 768d | 3 TB | PQ to 64 B/vec (fp32 would be 30 TB) |
| Document text (snippets) | 80 TB raw | 20 TB | zstd with trained dictionary |
| **Total per replica** | | **~65 TB** | of which ~20 TB is snippet text, not index |

---

## Posting list layout

Lists are stored in **blocks of 128 documents**. Each block carries a precomputed
**upper bound on the score any document in it can contribute**.

```
term "merger"
├── dictionary entry: df, cf, offset, block count
└── blocks:
    ┌─────────────────────────────────────────────┐
    │ block 0                                     │
    │   maxScore: 6.2      ← the key to everything│
    │   lastDocID: 84,213                         │
    │   docIDs:  [d-gaps, SIMD-BP128]             │
    │   freqs:   [bitpacked]                      │
    │   posOffset: → position stream              │
    └─────────────────────────────────────────────┘
    ┌─────────────────────────────────────────────┐
    │ block 1  maxScore: 5.9  lastDocID: 191,004  │
    └─────────────────────────────────────────────┘
```

**Positions live in a separate stream.** Most queries never need them — they are consulted
only for phrase matching and proximity scoring on documents that already survived retrieval.
Interleaving them with docIDs would force decoding 11 TB to answer queries that need 8 TB.

### Encoding choices

| Data | Encoding | Why |
| --- | --- | --- |
| docIDs | Delta (d-gaps) + SIMD-BP128 | Gaps are small; block-wise bitpacking vectorises |
| Frequencies | Bitpacked, per-block width | Mostly 1–3; a full byte is waste |
| Positions | Delta + varint | Accessed rarely, so favour size over decode speed |
| Skip data | Per-block lastDocID + maxScore | Enables the jump; ~1% overhead for a 10× win |

---

## Block-Max WAND — why this is affordable

The retrieval loop keeps a running threshold **θ** equal to the score of the current *k*-th
best result. For each candidate block alignment it sums the per-term block maxima; **if that
sum cannot exceed θ, the entire block is skipped without decoding a single posting.**

```
query: "black merger"     θ = 5.0 (score of current 10th-best)

block:        0     1     2     3     4     5     6     7
black  max: 1.9   2.1   1.8   2.2   2.0   1.7   2.1   1.9
merger max: 6.2     –     –   5.9     –   6.4     –   5.5
            ────  ────  ────  ────  ────  ────  ────  ────
sum:        8.1   2.1   1.8   8.1   2.0   8.1   2.1   7.4
            ✓     skip  skip  ✓     skip  ✓     skip  ✓

cursor:  [0] ──────jump──────▶ [3] ──jump──▶ [5] ──jump──▶ [7]
```

Four of eight blocks decoded. On real lists a rare term prunes **>99%** of a common term's
postings.

**The feedback loop is the important part:** every good document found raises θ, which prunes
harder, which is why top-10 is far cheaper than top-1000. Retrieval cost is superlinear in
*k*, and this is why [DISTRIBUTED-SEARCH](DISTRIBUTED-SEARCH.md) can shed load by lowering *k*.

---

## Auxiliary structures

| Structure | Purpose |
| --- | --- |
| **Term dictionary (FST)** | Term → posting offset. Front-coded, ~0.4 TB, in RAM per shard. |
| **Phrase index** | Posting lists for head bigrams ("new york"). Avoids position joins on the most common phrases. |
| **Anchor index** | Separate fielded lists. Text pointing *at* a doc, often better than its own text. |
| **Filter bitmaps** | Roaring bitmaps for language, country, safe-search, date bucket. Intersected before scoring. |
| **Forward index** | docID → fields, static rank, ML payload. Needed by L1 ranking and snippets. |
| **Delete bitmap** | Tombstones, applied at query time. See below. |

---

## Update model — log-structured

Immutable segments accumulate and merge on a size-tiered schedule, exactly like an LSM tree.
Deletions are tombstones in a delete bitmap applied at query time.

| Cadence | Contents | Freshness |
| --- | --- | --- |
| **Real-time layer** | In-memory, breaking news, high-λ pages | Seconds |
| **Hourly segments** | New and re-crawled documents | ~1 hour |
| **Daily merge** | Compaction, tombstone application, static-rank refresh | 1 day |
| **Monthly rebuild** | Schema changes, tokenizer changes, corpus re-scoring | 1 month |

A query merges results across all live segments plus the real-time layer at the leaf.

---

## The tradeoff you inherit from early termination

Block-max pruning works best when documents within a list are ordered by something correlated
with score — typically **static rank**.

That means **changing your static rank formula requires re-sorting every posting list, which
is a full rebuild.**

So the structure that makes retrieval cheap makes corpus-wide re-scoring a monthly operation.
If the anti-spam team needs to re-weight authority weekly, they cannot — which is precisely
why the demotion overlay exists ([FAILURE-HANDLING](FAILURE-HANDLING.md)). This tension is
inherent to the design, not an implementation gap.

---

## Target vs Build

| | Target | Build |
| --- | --- | --- |
| Format | Custom, block-max, SIMD-BP128 | Lucene via OpenSearch ([OPENSEARCH](OPENSEARCH.md)) |
| Retrieval | Own block-max WAND | Lucene's `BlockMaxMaxscore` (same idea, well-implemented) |
| Segments | Own generation artifact | Lucene segments + merge policy |
| Vectors | PQ + DiskANN | OpenSearch `knn_vector`, HNSW |
| Ceiling | 10¹⁰ docs | ~10⁹ docs before it stops being the right tool |

Lucene already implements most of this well. **Do not write your own index format for the
Build.** The Target format exists because at 10¹⁰ documents the per-document overhead and the
JVM stop being acceptable — not because Lucene's algorithms are wrong.

---

## Failure modes

| Failure | Consequence | Mitigation |
| --- | --- | --- |
| Corrupt segment | Wrong results, or crash, **in every replica** | Checksums per block; validate at build; shadow-traffic gate before rollout. Replication does not protect against this. |
| Merge storm | I/O saturation, latency spike | Rate-limit merges; schedule off-peak; cap concurrent merges per node. |
| Term dictionary too large for RAM | Every query pays a disk seek | Front-coding + FST; shard by term range if it still does not fit. |
| Skip data stale after merge | Wrong `maxScore` bounds → **wrong results, silently** | Recompute block maxima during merge. Assert monotonicity in tests. |
| Delete bitmap not applied | Deleted documents served | Apply at query time, not merge time. Legal requirement, not an optimisation. |
| Positions accessed for every query | 11 TB read instead of 8 TB | Keep the streams separate; only touch positions for phrase/proximity on survivors. |
