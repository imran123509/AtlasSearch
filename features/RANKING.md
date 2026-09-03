# Ranking

Ordering results by relevance. A funnel: **cheap models over many candidates, expensive
models over few.**

Cost per document rises ~100× at each stage and candidate count falls ~100×, so every stage
costs roughly the same in aggregate. That is the design principle.

Related: [BM25](BM25.md) · [PAGERANK](PAGERANK.md) · [DISTRIBUTED-SEARCH](DISTRIBUTED-SEARCH.md) · [SEARCH-ENGINE](SEARCH-ENGINE.md)

---

## The cascade

| Stage | Where | In | Out | Model | Latency |
| --- | --- | --- | --- | --- | --- |
| **L0** retrieval | leaf | 10¹⁰ | 1,000/shard | BM25F + static prior, block-max pruned; ANN for dense candidates | ~3 ms |
| **L1** shard rerank | leaf | 1,000 | 50/shard | Small GBDT, ~200 features from the per-doc payload | ~1 ms |
| **L2** merge rerank | root | ~3,000 | ~200 | Large GBDT / LambdaMART | ~8 ms |
| **L3** semantic | accelerator | 60 | 10 | Distilled cross-encoder | ~18 ms |

L1 runs *at the leaf* because moving 1,000 documents × 128 shards to the root would be
128,000 documents crossing the network per query. Filtering before the network hop is the
whole point.

---

## Signal families

### Query-independent (precomputed, in the per-doc payload, available at L1)

| Signal | Source |
| --- | --- |
| Link authority | [PAGERANK](PAGERANK.md) |
| Host trust | Aggregate authority + manual review + age |
| Spam probability | Classifier over content, link, and hosting features |
| Content quality | Readability, depth, originality, ad density |
| Page experience | Core Web Vitals, mobile viewport, HTTPS |
| Freshness | Publish date, last substantive change |

### Query-dependent lexical

- **[BM25F](BM25.md)** over `title` / `body` / `anchors` / `url` as separate fields with
  learned weights.
- **Term dependency (proximity).** A sequential-dependence model scoring adjacent and
  unordered query-term windows. *Proximity matters far more than a bag-of-words score
  suggests* — this is one of the highest-value additions to a plain BM25 baseline.

```
score = λ₁·Σ unigram   +  λ₂·Σ ordered bigram window
      + λ₃·Σ unordered window(8)
typical:  λ = (0.85, 0.10, 0.05)
```

### Semantic

Dense bi-encoder similarity as a retrieval candidate source and an L2 feature; cross-encoder
relevance at L3. See the tradeoff section below.

### Behavioural

Click-through and dwell, aggregated per (query, document) and per (query-class, host).

**Must be de-biased.** Raw CTR encodes *position* far more strongly than relevance — the
result at position 1 gets clicked because it is at position 1. Use a position-aware click
model (PBM or DBN) to separate examination probability from relevance:

```
P(click) = P(examine | position) × P(relevant | query, doc)
```

You want the second factor. Using raw CTR trains the model that "whatever is on top is good."

---

## Query understanding

Runs **in parallel with the cache lookup**, not before it.

| Step | Notes |
| --- | --- |
| Spelling correction | Noisy-channel model trained on query reformulation pairs from logs |
| Segmentation | `newyorktimes` → `new york times` |
| Intent classification | Navigational / informational / transactional (Broder's taxonomy) |
| Entity linking | Resolve mentions to a knowledge base |
| Locale + language | From query text, user locale, and IP region |
| Freshness demand | Does this query deserve recency at all? |

**Expansion is applied conservatively.** It reliably improves recall and reliably damages
precision, so it is gated on retrieval returning too little rather than applied by default.

---

## Evaluation

You cannot improve what you cannot measure, and ranking is unusually easy to fool yourself
about.

| Method | Use | Caveat |
| --- | --- | --- |
| **NDCG@10** on human-rated pools | Offline iteration | Pool bias: documents nobody rated score 0 even if excellent |
| **Team-draft interleaving** | Online A/B, high sensitivity | Needs live traffic; ~10× more sensitive than A/B |
| **A/B with sequential testing** | Final gate | Slow; needs guardrail metrics |
| **Holdback (0.1%)** | Measure what optimisations cost | The only defence against silent regressions |

### Guardrail metrics — check these on every launch

Abandonment rate · reformulation rate · time to first click · results-per-query · latency p99
· spam-report rate. A ranking change that improves NDCG while raising reformulation rate is
not an improvement.

---

## Tradeoffs and open problems

### Dense retrieval is not the backbone here, and that may be wrong in three years

Embeddings are a **complementary candidate source**, not the primary index. Reasons:

- Degrades badly on **exact identifiers and rare entities** — a bi-encoder will not reliably
  retrieve `0x80070643`.
- No cheap **boolean filtering**.
- Decisively: changing the embedding model means **re-embedding 10¹⁰ documents**. That puts
  retrieval on a quarterly release cadence while the ranker ships weekly.

**Ruling:** hybrid, lexical-primary, fused with reciprocal rank fusion. But keep the fusion
layer swappable and the vector store physically separable, and do not let dense scores leak
into components that assume lexical semantics — that coupling is what would make the eventual
migration impossible.

### The design assumes click data it will not have

Behavioural signals appear in the L2 feature list as though available. For a new engine they
are not. The first year has to run on content, links, and a relevance model bootstrapped from
human or LLM-generated judgements.

Then, once clicks arrive, they create a **rich-get-richer loop**: the top result stays on top
because it is on top. Correcting it needs deliberate exploration — randomised or
bandit-driven position perturbation — that costs measurable user satisfaction today to keep
the model healthy tomorrow. Most teams quietly skip it and slowly ossify.

Both ends are uncomfortable and neither usually appears in the architecture diagram.

### Anti-spam is a third of the work and none of the diagram

Everything above assumes documents are not actively lying. They are. Without link-farm
detection, cloaking detection, doorway-page detection, and machine-generated-content scoring,
this is an efficient link-farm delivery service.

Worse: adversaries adapt in **hours** while the index pipeline moves in **days**. Anti-spam
therefore *requires* a fast-path demotion overlay that bypasses the index build — breaking
the clean two-plane separation the whole architecture rests on. That is not a detail.

---

## Target vs Build

| | Target | Build |
| --- | --- | --- |
| L0 | Custom block-max WAND | OpenSearch `multi_match` + `rank_feature` |
| L1 | GBDT at the leaf | Skipped — corpus small enough |
| L2 | LambdaMART, ~500 features | LightGBM, ~40 features |
| L3 | Distilled cross-encoder on accelerator | `ms-marco-MiniLM` cross-encoder, top 20, CPU |
| Clicks | De-biased click model | None initially — bootstrap with LLM-judged relevance |

Start with **BM25F + PageRank prior + proximity**. That baseline is stronger than most
people expect and is the thing every later stage is measured against.
