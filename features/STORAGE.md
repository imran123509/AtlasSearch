# Storage

Four stores with genuinely different requirements. Conflating them is the most common
architectural mistake in this part of the system.

Related: [INVERTED-INDEX](INVERTED-INDEX.md) · [KAFKA](KAFKA.md) · [DISTRIBUTED-INDEXING](DISTRIBUTED-INDEXING.md)

---

## The four stores

| Store | Contents | Access | Size (target) | Durability |
| --- | --- | --- | --- | --- |
| **Blob** | Raw fetched bodies (WARC) | Write-once, batch-read | 1 PB raw / 200 TB gz per generation | High — expensive to re-crawl |
| **Document** | Extracted text for snippets | Random read, latency-critical | 20 TB compressed | Rebuildable from blob |
| **Index** | Postings, positions, vectors | Random read, latency-critical | 45 TB | Rebuildable |
| **Metadata** | Frontier, leases, shard map, generation pointers | Read-write, consistent | < 500 GB | **Must be consistent** |

Only the last one needs real consistency, and it is deliberately tiny and **off the query
path**.

---

## Blob store — raw pages

```
s3://atlas-raw/{yyyy}/{mm}/{dd}/{shard}/{batch_id}.warc.zst
```

- **WARC format** — the archival standard. Stores request, response, headers, and timestamps
  together, so a page can be fully re-processed later.
- **Batch small pages into large objects.** 90 KB objects mean 400 M PUTs/day; per-request
  cost and metadata overhead dominate. Batch to ~500 MB with an offset index.
- **Content-addressed by hash**, so identical bodies stored once.

### Retention and tiering

| Age | Tier | Purpose |
| --- | --- | --- |
| 0–30 d | Hot (S3 Standard) | Reprocessing, parser bug replay |
| 30 d–1 y | Infrequent access | ML training sets, change analysis |
| > 1 y | Glacier / deleted | Only sampled crawls kept |

Keeping every generation forever is the fastest way to make storage the dominant line item.
Keep the current generation hot, sample the history.

---

## Document store — snippet text

This is the store people forget, and it is **~20 TB at target scale**, roughly a third of the
serving footprint.

```
key   = doc_id
value = zstd(extracted_text + fields + metadata)   with a trained dictionary
```

- Sharded by `doc_id` — a **separate fanout** from the index fanout, with its own tail
  behaviour ([SEARCH-ENGINE](SEARCH-ENGINE.md)).
- Latency-critical: it sits in the snippet path, which is the p99.
- **Trained zstd dictionary matters here.** Documents are small (8 KB) and share vocabulary; a
  dictionary trained on a corpus sample improves the ratio from ~3:1 to ~4.5:1 on this data.

Any capacity plan that sizes "the index" and omits this store is short by about a third.

---

## Index store

Per-shard immutable segment files, mounted read-only by leaves.

| Tier | Medium | Why |
| --- | --- | --- |
| 0 | DRAM | 3 ms leaf RPC budget leaves no room for a page fault |
| 1 | DRAM + NVMe | Hot postings cached, cold on disk |
| 2 | NVMe | 12 ms budget tolerates a seek |

Distribution: object store is the source of truth; hosts download and validate on generation
flip, then serve from local disk. Never serve directly from object storage — the latency
variance is fatal to p99.

---

## Metadata store

The only place needing real consistency. Raft- or Paxos-backed (etcd, Spanner-like, or
Postgres with synchronous replication).

| Data | Notes |
| --- | --- |
| Generation pointers | Which generation each tier serves |
| Shard map | shard → host assignment |
| Crawl leases | TTL-based |
| Deny-list | Legal removals — **audited** |
| Analyzer / schema versions | Checked at query time |

**Keep it under 500 GB and never let a user query depend on it synchronously.** Serving hosts
cache the shard map and generation pointer locally and refresh in the background; a metadata
store outage must degrade to "keep serving what you have", not to an outage.

---

## Sizing

| Store | Raw | Stored | Notes |
| --- | --- | --- | --- |
| Blob, current generation | 1 PB | 200 TB | zstd ~5:1 on HTML |
| Blob, 1 y history | ~365 PB | ~2 PB | Sampled, not complete |
| Document text | 80 TB | 20 TB | Trained-dictionary zstd |
| Index (postings + positions + aux) | — | 45 TB | See [INVERTED-INDEX](INVERTED-INDEX.md) |
| Metadata | — | < 0.5 TB | Consistent store |
| **Per serving replica** | | **~65 TB** | Index + document text |

Earlier drafts of this design said "1 EB of raw HTML". That is wrong by three orders of
magnitude: 10¹⁰ × 90 KB = **0.9 PB**, not 1 EB. Worth stating because the mistake changes
whether the blob store is a line item or the entire budget.

---

## Target vs Build

| | Target | Build |
| --- | --- | --- |
| Blob | S3 + Glacier tiering | MinIO or local disk |
| Document | Custom sharded KV | OpenSearch stored fields |
| Index | Local NVMe, object-store distribution | OpenSearch data nodes |
| Metadata | etcd / Spanner-like | Postgres |
| Total | ~2.3 PB | ~200 GB |

At Build scale, OpenSearch holds the index *and* the document text (stored fields) *and*
serves snippets via `highlight`. That collapses three stores into one, which is right at 10⁷
documents and wrong at 10¹⁰ — the snippet fanout and the index fanout have different shard
keys and different latency profiles, and forcing them onto one topology wastes both.

---

## Failure modes

| Failure | Consequence | Mitigation |
| --- | --- | --- |
| Document store omitted from sizing | Serving footprint short by ~⅓ | Size it explicitly |
| Serving directly from object storage | p99 destroyed by latency variance | Download to local NVMe, validate, then serve |
| Small objects in blob store | PUT cost and metadata overhead dominate | Batch to ~500 MB with an offset index |
| Metadata store on the query path | Its outage becomes a search outage | Cache locally, refresh in background |
| Metadata store grows unbounded | Consensus latency rises; the store becomes fragile | Keep < 500 GB; push bulk data elsewhere |
| No blob retention policy | Storage becomes the dominant cost | Tier by age; sample history |
| Corrupt segment downloaded | Wrong results in that replica | Checksum in the manifest; validate before mount |
| Blob store lost | Cannot reprocess; must re-crawl | This is the one store with real durability requirements |
