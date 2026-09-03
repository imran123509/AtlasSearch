# OpenSearch

The **Build** search backend. Lucene under the hood, so most of
[INVERTED-INDEX](INVERTED-INDEX.md) applies directly — block-max retrieval, segment merges,
immutable segments.

This doc is also where the Build/Target boundary is most concrete: **OpenSearch is the right
answer up to roughly 10⁹ documents and the wrong answer above it.**

Related: [INVERTED-INDEX](INVERTED-INDEX.md) · [BM25](BM25.md) · [TOKENIZATION](TOKENIZATION.md) · [SHARDING](SHARDING.md)

---

## Index mapping

```json
{
  "settings": {
    "index": {
      "number_of_shards": 3,
      "number_of_replicas": 1,
      "refresh_interval": "30s",
      "similarity": { "default": { "type": "BM25", "k1": 1.2, "b": 0.75 } }
    },
    "analysis": {
      "analyzer": {
        "atlas_en": {
          "type": "custom",
          "char_filter": ["html_strip"],
          "tokenizer": "standard",
          "filter": ["icu_normalizer", "icu_folding", "lowercase",
                     "keyword_repeat", "english_stemmer", "unique_stem"]
        }
      },
      "filter": {
        "english_stemmer": { "type": "stemmer", "language": "english" },
        "unique_stem":     { "type": "unique", "only_on_same_position": true }
      }
    }
  },
  "mappings": {
    "properties": {
      "url":        { "type": "keyword" },
      "domain":     { "type": "keyword" },
      "title":      { "type": "text", "analyzer": "atlas_en" },
      "headings":   { "type": "text", "analyzer": "atlas_en" },
      "body":       { "type": "text", "analyzer": "atlas_en" },
      "anchors":    { "type": "text", "analyzer": "atlas_en" },
      "url_text":   { "type": "text", "analyzer": "atlas_en" },
      "lang":       { "type": "keyword" },
      "published":  { "type": "date" },
      "pagerank":   { "type": "rank_feature" },
      "quality":    { "type": "rank_feature" },
      "spam_score": { "type": "rank_feature", "positive_score_impact": false },
      "simhash":    { "type": "keyword", "index": false },
      "embedding":  { "type": "knn_vector", "dimension": 384,
                      "method": { "name": "hnsw", "space_type": "cosinesimil" } }
    }
  }
}
```

### `keyword_repeat` + `unique` — index surface form *and* stem

This is the [TOKENIZATION](TOKENIZATION.md) "index both" rule, implemented. `keyword_repeat`
duplicates each token, the stemmer stems one copy, `unique only_on_same_position` drops the
duplicate when stemming was a no-op. Recall from the stem, precision from the surface form,
~30% more postings.

---

## Query

```json
{
  "query": {
    "bool": {
      "must": [{
        "multi_match": {
          "query": "block max wand",
          "type": "cross_fields",
          "fields": ["anchors^10", "title^8", "headings^3", "url_text^2.5", "body^1"],
          "operator": "or",
          "minimum_should_match": "2<70%"
        }
      }],
      "should": [
        { "rank_feature": { "field": "pagerank", "log": { "scaling_factor": 4 } } },
        { "rank_feature": { "field": "quality",  "saturation": {} } },
        { "match_phrase": { "body": { "query": "block max wand", "slop": 4, "boost": 3 } } }
      ],
      "must_not": [
        { "rank_feature": { "field": "spam_score", "saturation": { "pivot": 0.7 } } }
      ],
      "filter": [{ "term": { "lang": "en" } }]
    }
  },
  "size": 10,
  "timeout": "800ms",
  "terminate_after": 100000,
  "collapse": { "field": "domain", "max_concurrent_group_searches": 4 },
  "highlight": {
    "fields": { "body": { "fragment_size": 160, "number_of_fragments": 1 } }
  }
}
```

### `cross_fields`, not `most_fields`

`most_fields` sums per-field BM25 scores — each field saturates independently, so a term
stuffed once per field bypasses saturation ([BM25](BM25.md)). `cross_fields` blends term
statistics across fields before scoring, which approximates BM25F.

This is a real scoring bug in most tutorial configurations.

### The `should` clauses do the ranking work

- `rank_feature` with `log` for PageRank — additive in log space, so a high-authority document
  cannot overwhelm a weak textual match.
- `match_phrase` with `slop` is the cheap proximity signal, and it is worth more than most
  people expect ([RANKING](RANKING.md)).
- `collapse` on `domain` gives result diversity — the SERP-level problem that de-duplication
  does not solve.

---

## Operations

```yaml
# Heap: half of RAM, hard cap 31 GB (compressed oops boundary)
OPENSEARCH_JAVA_OPTS: "-Xms31g -Xmx31g"
# The other half is page cache — that is what actually serves postings
```

| Setting | Value | Why |
| --- | --- | --- |
| `refresh_interval` | `30s` (indexing: `-1`) | Default 1s creates tiny segments and constant merge pressure |
| `number_of_replicas` | `0` during bulk load, then raise | Halves indexing work |
| `translog.durability` | `async` for the crawl index | Rebuildable data; fsync-per-write is wasted |
| Shard size | 10–50 GB | Same rule as [SHARDING](SHARDING.md) |
| `terminate_after` | 100,000 | Bounds worst-case query cost |

### Bulk indexing

```
bulk size    5–15 MB per request (not a document count)
concurrency  ~2× data nodes
retry        exponential backoff on 429 — 429 means "slow down", not "failed"
```

### Generation swap via alias

```
create docs-v7 → bulk load → validate → atomically swap the alias
POST /_aliases { "actions": [
  { "remove": { "index": "docs-v6", "alias": "docs" } },
  { "add":    { "index": "docs-v7", "alias": "docs" } } ]}
```

Atomic. `docs-v6` stays mounted for rollback. This is the Build-scale version of the
generation manifest in [DISTRIBUTED-INDEXING](DISTRIBUTED-INDEXING.md), and it is the same
idea in one API call.

---

## Where OpenSearch stops being the right tool

Not because Lucene's algorithms are wrong — they are excellent, and the Target format uses the
same ideas. It stops for operational reasons:

| Limit | Symptom | Roughly |
| --- | --- | --- |
| JVM heap 31 GB/node | Term dictionary + field data no longer fit; GC pauses enter p99 | ~10⁸–10⁹ docs/node |
| Per-document overhead | Storage and merge cost grow faster than the corpus | ~10⁹ docs |
| No tiering primitive | Every query fans out to every shard; cannot spill | When fanout cost dominates |
| Coordinator merge | Single-node merge of `shards × size` candidates | ~200+ shards |
| Segment merges | Full-corpus merges take days | ~10¹⁰ docs |

**The migration is a rewrite of the serving path, not a scale-up.** Plan for it as a rewrite.
The parts that carry over: analyzers, ranking features, evaluation harness, and the
alias-swap discipline. The parts that do not: the index format, the fanout, the merge.

---

## Failure modes

| Failure | Consequence | Mitigation |
| --- | --- | --- |
| `most_fields` instead of `cross_fields` | Per-field saturation; keyword stuffing works | Use `cross_fields` |
| Heap > 31 GB | Loses compressed oops; GC pauses spike p99 | Cap at 31 GB; give the rest to page cache |
| `refresh_interval: 1s` during bulk load | Merge storm, indexing crawls | `-1` during load, `30s` after |
| Replicas enabled during bulk load | ~2× indexing cost | `0` during load, raise after |
| Deep pagination | Quadratic cost, coordinator OOM | Cap `from`; use `search_after` ([SEARCH-API](SEARCH-API.md)) |
| Mapping explosion (dynamic fields) | Cluster state grows until the cluster degrades | `dynamic: strict` |
| No `timeout` / `terminate_after` | One pathological query saturates the cluster | Set both |
| Reindex without alias | Downtime during the swap | Always swap aliases, never rename |
| Analyzer changed in place | Old documents unreachable, silently | New index + reindex + alias swap. Never in place. |
