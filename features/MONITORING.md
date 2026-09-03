# Monitoring

Two categories, and most teams only build the first.

| Category | Question | Detection time |
| --- | --- | --- |
| **Systems** | Is it up and fast? | Seconds |
| **Quality** | Is it returning good results? | Days — if you instrumented for it. Never, if you didn't. |

A search engine can be 100% available, p99 within SLO, zero errors, and returning garbage.
Systems monitoring will report everything green.

Related: [FAILURE-HANDLING](FAILURE-HANDLING.md) · [SEARCH-ENGINE](SEARCH-ENGINE.md) · [RANKING](RANKING.md)

---

## SLOs

| Service | SLI | Objective | Window |
| --- | --- | --- | --- |
| Search API | Availability (non-5xx) | 99.9% | 30 d |
| Search API | Latency p99 | < 400 ms | 30 d |
| Search API | Latency p50 | < 120 ms | 30 d |
| Index freshness | Tier-0 docs re-crawled < 24 h | 95% | 7 d |
| Pipeline | Fetch → searchable | p95 < 6 h | 7 d |
| Quality | NDCG@10 on the rated set | ≥ baseline − 1% | per release |

Error budget: 99.9% over 30 days = **43 minutes**. Spending it is fine; spending it without
learning anything is not.

---

## Systems metrics

### Search path

```
search_requests_total{status, cache, degraded_rung}
search_latency_seconds{stage}          histogram — per stage, not just total
search_tiers_searched{tier}            spill rate
search_partial_results_total           shards missing from a response
search_cache_hit_ratio
search_results_empty_total             zero-result queries
```

**Per-stage latency histograms are essential.** A total-latency alert tells you something is
slow. Per-stage tells you it is the snippet fanout, which is where it usually is
([SEARCH-ENGINE](SEARCH-ENGINE.md)).

### Crawl path

```
crawl_fetches_total{status_class}
crawl_fetch_duration_seconds
crawl_robots_denied_total
crawl_politeness_violations_total      ← must be 0. Alert on any.
crawl_frontier_size
crawl_frontier_idle_fetchers           back-queue starvation
crawl_render_decisions_total{rendered}
```

### Pipeline

```
kafka_consumer_lag{topic, group}       ← the primary pipeline health signal
index_generation_age_seconds
index_build_duration_seconds{stage}
index_docs_indexed_total
```

---

## Quality metrics — the ones nobody builds

### 1. The tiering holdback

0.1% of traffic bypasses tier logic and queries all tiers
([DISTRIBUTED-SEARCH](DISTRIBUTED-SEARCH.md)).

```
quality_holdback_ndcg_gap
```

**This is the price tiering is charging you.** When the sufficiency threshold misfires, the
user gets a worse answer, no error is raised, and no other metric moves. This gap is the only
signal. It needs a named owner and a review cadence, not just a dashboard panel.

### 2. Zero-result and abandonment rates

```
search_results_empty_ratio             rising = index or analyzer problem
search_abandonment_ratio               no click within 30 s
search_reformulation_ratio             user immediately searches again
search_time_to_first_click_seconds
```

Reformulation rate is the most sensitive early warning for a bad ranking deploy. It moves
within hours; NDCG on a rated set takes days.

### 3. Corpus health

```
index_docs_by_tier
index_docs_by_language
index_spam_score_distribution
crawl_content_dedup_ratio              sudden change = parser or dedup bug
parse_extracted_text_length_p50        sudden drop = boilerplate remover ate the content
render_classifier_negative_rate        rising = sites silently missing from the index
```

`parse_extracted_text_length_p50` is worth singling out. If the boilerplate remover starts
eating main content after a deploy, **nothing else alerts** — documents index fine, queries
succeed, latency is normal, and result quality quietly collapses.

---

## Alerts

Alert on **symptoms users feel**, not on causes.

| Alert | Condition | Severity |
| --- | --- | --- |
| SLO burn rate | 14.4× over 1 h (2% of budget) | Page |
| Politeness violation | any | Page — legal/ethical exposure |
| Degradation rung ≥ 3 | > 5 min | Page |
| Partial results | > 1% of queries for 10 min | Page |
| Kafka lag | > 6 h of production | Page — retention loss is coming |
| Index generation age | > 12 h | Page — build pipeline is stuck |
| Holdback NDCG gap | > 2% for 24 h | Ticket |
| Reformulation rate | +15% week over week | Ticket |
| Extracted text length p50 | −30% day over day | Ticket |
| Zero-result ratio | > 3% | Ticket |

### Multi-window burn-rate alerting

```
page:   14.4× burn over 1 h  AND  6× over 6 h    (fast burn, confirmed)
ticket:  3× burn over 24 h   AND  1× over 3 d    (slow burn)
```

The two-window requirement suppresses the single-spike false positives that make people
ignore pages.

---

## Tracing

Distributed tracing across the fanout, sampled at 1% plus 100% of slow requests:

```
search
├── query_understanding        6 ms
├── cache_lookup               1 ms   MISS
├── fanout_tier0              14 ms
│   ├── leaf_007               8 ms
│   ├── leaf_031              12 ms
│   └── leaf_099              14 ms   ← the straggler that set the stage latency
├── rerank_l2                  8 ms
├── rerank_l3                 18 ms
└── snippets                  34 ms   ← usually the largest span
    ├── docserver_012         22 ms
    └── docserver_044         34 ms
```

**Tail-based sampling**, not head-based. You want the slow traces, and head-based sampling
throws away the interesting 1% along with everything else.

Propagate trace context through Kafka too — a document's journey from fetch to searchable
crosses four services and two topics, and "why did this page take 9 hours to appear" is
otherwise unanswerable.

---

## Dashboards

| Dashboard | Audience | Contents |
| --- | --- | --- |
| **Service health** | On-call | SLO burn, latency by stage, error rate, degradation rung |
| **Pipeline** | On-call | Kafka lag by topic, generation age, build stage durations |
| **Crawl** | Crawl owner | Fetch rate, status distribution, frontier size, politeness |
| **Quality** | Ranking owner | Holdback gap, NDCG, reformulation, zero-result, corpus health |
| **Cost** | Everyone | Fetches, storage growth, host count, spill rate |

The quality dashboard is the one that will not get built unless someone owns it. Assign it.

---

## Target vs Build

| | Target | Build |
| --- | --- | --- |
| Metrics | Prometheus + long-term store | Prometheus + Grafana |
| Tracing | Tail-sampled, own collector | OpenTelemetry → Jaeger |
| Logs | Structured, sampled, centralised | `stdout` → Loki |
| Quality | Continuous holdback + rated pools | Weekly manual eval on ~200 queries |

Even at Build scale, **run the rated-query evaluation before every ranking change**. 200
hand-rated queries and an NDCG script is a few hours of work and it is the difference between
knowing your ranking improved and believing it did.
