# AtlasSearch

A web search engine, designed to scale toward **10 billion pages** and built in stages
starting from a corpus small enough to run on one machine.

---

## The two-layer framing

This repository documents two things at once, and every feature doc keeps them separate:

| Layer | What it is | Where it runs |
| --- | --- | --- |
| **Target** | The 10¹⁰-document architecture. Custom index format, tiered shards, block-max retrieval, four-stage ranking cascade. | ~27,000 hosts. Nobody starts here. |
| **Build** | What we actually implement now. OpenSearch + Kafka + Redis, one region, ~10⁷–10⁸ documents. | Laptop → a handful of nodes. |

Keeping both visible matters because **the Build cannot become the Target by growing**.
OpenSearch stops being the right answer somewhere around 10⁹ documents, and the migration
is a rewrite of the serving path, not a scale-up. Each doc has a `Target vs Build` section
saying exactly where its component diverges.

**Design rationale, full sizing arithmetic, and the argument against this architecture:**
[Ten Billion Pages](https://claude.ai/code/artifact/cac7a961-a7c9-4b7b-bbfd-daac8fa57d5d)

---

## Structure

```
AtlasSearch/
├── README.md
├── features/          design docs — one per subsystem
├── crawler/           fetch, frontier, robots, dedup
├── indexer/           parse, tokenize, score, build index
├── search-api/        query understanding, retrieval, ranking, snippets
├── frontend/          web UI
└── infrastructure/    docker, k8s, terraform, dashboards
```

---

## Feature docs

### Crawl
| Doc | Covers |
| --- | --- |
| [WEB-CRAWLER](features/WEB-CRAWLER.md) | Fetch loop, politeness, robots, DNS, fetch budget |
| [URL-FRONTIER](features/URL-FRONTIER.md) | Front/back queue design, priority vs politeness, recrawl scheduling |
| [HTML-PARSER](features/HTML-PARSER.md) | Parse, boilerplate removal, JS rendering decision, link extraction |
| [URL-DE-DUPLICATION](features/URL-DE-DUPLICATION.md) | Canonicalisation, seen-URL test at 5×10¹¹ |
| [CONTENT-DE-DUPLICATION](features/CONTENT-DE-DUPLICATION.md) | SimHash, shingling, canonical election |

### Index & rank
| Doc | Covers |
| --- | --- |
| [INVERTED-INDEX](features/INVERTED-INDEX.md) | Posting layout, compression, block-max WAND, segment merges |
| [TOKENIZATION](features/TOKENIZATION.md) | Normalisation, stemming, CJK segmentation, index/query symmetry |
| [RANKING](features/RANKING.md) | Four-stage cascade, signal families, evaluation |
| [BM25](features/BM25.md) | Scoring maths, BM25F fields, parameter tuning |
| [PAGERANK](features/PAGERANK.md) | Link graph, iteration, spam resistance, why it decayed as a signal |

### Serve
| Doc | Covers |
| --- | --- |
| [SEARCH-API](features/SEARCH-API.md) | HTTP contract, query parsing, pagination, rate limits |
| [SEARCH-ENGINE](features/SEARCH-ENGINE.md) | End-to-end query path and latency budget |
| [DISTRIBUTED-SEARCH](features/DISTRIBUTED-SEARCH.md) | Scatter-gather, tiering, tail latency, hedging |
| [SHARDING](features/SHARDING.md) | Document vs term partitioning, tier layout, rebalancing |

### Pipeline
| Doc | Covers |
| --- | --- |
| [KAFKA](features/KAFKA.md) | Topics, partitioning, ordering, backpressure |
| [REDIS](features/REDIS.md) | Rate limiters, dedup filters, result cache, robots cache |
| [DISTRIBUTED-CRAWLER](features/DISTRIBUTED-CRAWLER.md) | Host-affinity sharding, leases, coordination |
| [DISTRIBUTED-INDEXING](features/DISTRIBUTED-INDEXING.md) | Shuffle, segment build, generation publication |

### Storage
| Doc | Covers |
| --- | --- |
| [STORAGE](features/STORAGE.md) | Blob store, document store, metadata store, sizing |
| [OPENSEARCH](features/OPENSEARCH.md) | Mappings, analyzers, query DSL — and where it stops working |

### Operations
| Doc | Covers |
| --- | --- |
| [DOCKER](features/DOCKER.md) | Images, compose topology, local dev |
| [KUBERNETES](features/KUBERNETES.md) | Workload types, statefulsets, autoscaling |
| [MONITORING](features/MONITORING.md) | SLOs, metrics, quality instrumentation |
| [FAILURE-HANDLING](features/FAILURE-HANDLING.md) | Degradation ladder, correlated failure, query-of-death |

---

## Scale targets

| Quantity | Build (now) | Target |
| --- | --- | --- |
| Indexed documents | 10⁷ | 10¹⁰ |
| Known URLs | 10⁸ | 5 × 10¹¹ |
| Fetches / day | 2 × 10⁶ | 8.8 × 10⁸ |
| Peak QPS | 100 | 150,000 |
| Index size | 60 GB | ~65 TB / replica |
| p50 / p99 latency | 200 / 800 ms | 120 / 400 ms |
| Hosts | 3 | ~27,000 |

---

## Architecture in one paragraph

Crawl and serve are **separate planes that never talk to each other**. The crawler writes
raw pages to a blob store; the indexer turns them into an immutable, versioned index
generation; the serving fleet mounts that generation read-only. This makes serving a
read-only problem — no write path, no consensus on the query path, replication is a file
copy, rollback is a pointer flip. Two overlays break the separation on purpose: a **legal
deny-list** and a **spam demotion list**, both applied at query time, because both must take
effect faster than a rebuild.

## Three ideas that survive at any scale

1. **Immutable versioned index artifact** — serving becomes read-only; rollback is a pointer flip.
2. **Holdback measurement** — 0.1% of traffic bypasses every optimisation, so you can measure
   what your optimisations cost in quality. Without it, tiering is a cost saving of unknown price.
3. **Explicit degradation ladder** — overload behaviour is a designed artifact with named rungs,
   not an emergent property of timeouts.

Everything else follows from the corpus size.

---

## Status

Design phase. Feature docs are written; service directories are scaffolds.
