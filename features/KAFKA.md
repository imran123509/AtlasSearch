# Kafka

The pipeline backbone. Decouples crawl from parse from index, absorbs burst mismatch between
stages, and provides replay when a downstream stage has a bug.

Related: [DISTRIBUTED-CRAWLER](DISTRIBUTED-CRAWLER.md) · [DISTRIBUTED-INDEXING](DISTRIBUTED-INDEXING.md) · [STORAGE](STORAGE.md)

---

## Topics

| Topic | Key | Partitions | Retention | Payload |
| --- | --- | --- | --- | --- |
| `urls.discovered` | `registrable_domain` | 256 | 7 d | URL + source + anchor + depth |
| `urls.scheduled` | `registrable_domain` | 256 | 1 d | URL + priority + lease |
| `pages.fetched` | `doc_id` | 512 | 3 d | **blob pointer** + headers + fetch metadata |
| `pages.parsed` | `doc_id` | 512 | 3 d | Extracted text, fields, links |
| `links.extracted` | `target_domain` | 256 | 7 d | Edge (source, target, anchor, rel) |
| `docs.indexable` | `doc_id` | 512 | 7 d | Final document ready for segment build |
| `docs.deleted` | `doc_id` | 512 | 30 d | Tombstones — long retention, legal audit |
| `index.generations` | `—` | 1 | forever | Generation manifests, compacted |

---

## Never put page bodies in Kafka

`pages.fetched` carries a **pointer**, not the HTML:

```json
{
  "doc_id": "d:8f2a91c4",
  "url": "https://example.org/article",
  "blob": "s3://atlas-raw/2026/09/02/8f2a91c4.warc.gz",
  "content_hash": "sha256:...",
  "fetched_at": "2026-09-02T14:22:01Z",
  "status": 200,
  "etag": "W/\"1a2b3c\"",
  "content_type": "text/html; charset=utf-8",
  "size_bytes": 91204
}
```

At 400 M full fetches/day × 90 KB, putting bodies in Kafka means **36 TB/day** through the
brokers, replicated 3× = 108 TB/day of disk write. Kafka is a log, not a blob store. Write
bodies to object storage ([STORAGE](STORAGE.md)) and pass the key.

Keep messages under ~100 KB. `pages.parsed` carries extracted text, which is ~8 KB — that is
fine.

---

## Partitioning and ordering

Kafka guarantees ordering **within a partition only**. Every key choice above exists to put
the messages that must be ordered into the same partition.

| Topic | Key | Why |
| --- | --- | --- |
| `urls.*` | `registrable_domain` | All URLs for a host land on one partition → one consumer → **host politeness is enforceable in-process** without distributed coordination |
| `pages.*` | `doc_id` | All events for a document are ordered; fetch-then-parse-then-delete cannot race |
| `links.extracted` | `target_domain` | Anchor text for a target arrives at the consumer that owns that target |

**Keying `urls.*` by domain is the whole reason the crawler can be politely distributed.**
Partition ownership becomes host ownership. See [DISTRIBUTED-CRAWLER](DISTRIBUTED-CRAWLER.md).

### The skew this causes

Domain keying means a huge site concentrates on one partition. `wikipedia.org` alone can
saturate a consumer.

```
if domain_volume > partition_capacity:
    key = f"{domain}#{hash(url) % n_subpartitions}"
```

Sub-partition only for domains on an explicit high-volume list, and hold the politeness
budget for those in Redis rather than in-process — the only case where a distributed rate
limiter is required.

---

## Delivery semantics

**At-least-once everywhere.** Exactly-once is available in Kafka but costs throughput and
operational complexity, and every consumer here can be made idempotent instead:

| Consumer | Idempotency key |
| --- | --- |
| Fetcher | Duplicate fetch = one wasted page load. Harmless. |
| Parser | `doc_id + content_hash` — same input, same output |
| Indexer | `doc_id` upsert into the segment |
| Link aggregator | `(source, target)` set semantics |

**Commit offsets after the side effect, never before.** Committing first turns a consumer
crash into silent data loss, and in this pipeline that means documents that were fetched,
never indexed, and never retried — invisible unless you go looking.

---

## Backpressure

The stage rates do not match, and that is the point of having a log between them:

```
crawler   10,200 msg/s ──▶ [urls.scheduled] ──▶ fetcher
fetcher   10,200 msg/s ──▶ [pages.fetched]  ──▶ parser    parser is ~3× slower
parser     3,500 msg/s ──▶ [pages.parsed]   ──▶ indexer   indexer batches
```

Lag on `pages.fetched` is the **primary health signal for the whole pipeline**. Growing lag
means the parser cannot keep up, which means the crawler must slow down — otherwise retention
expires and pages are lost.

```
if consumer_lag(pages.fetched) > 6 h of production:
    reduce crawl rate                    ← close the loop
    alert
```

That feedback loop must exist. Without it the crawler happily fills the log until retention
drops data on the floor, and the loss is silent.

---

## Configuration that matters

```properties
# Durability — the pipeline is restartable, but do not lose committed data
acks=all
min.insync.replicas=2
replication.factor=3
unclean.leader.election.enable=false     # never trade correctness for availability here

# Throughput
compression.type=zstd                    # text compresses ~4:1
batch.size=131072
linger.ms=50                             # batching matters far more than 50 ms of latency
max.in.flight.requests.per.connection=5
enable.idempotence=true                  # free; prevents duplicates from producer retries

# Consumers
max.poll.records=500
max.poll.interval.ms=600000              # parse can be slow; do not get kicked from the group
auto.offset.reset=earliest
enable.auto.commit=false                 # commit manually, after the side effect
```

`unclean.leader.election.enable=false` is the one to be sure about. Enabling it lets an
out-of-sync replica become leader and silently truncate committed messages.

---

## Replay

The reason for a log rather than a queue. When the parser has a bug:

```
1. fix the parser
2. reset the consumer group offset on pages.fetched to before the bad deploy
3. reprocess — blob pointers still resolve, bodies are in object storage
4. indexer upserts by doc_id, so reprocessing is idempotent
```

Retention on `pages.fetched` is 3 days, which sets the **maximum blast radius of a parser
bug**. That is the actual reason for the retention number — not storage cost.

---

## Target vs Build

| | Target | Build |
| --- | --- | --- |
| Brokers | 30+, tiered storage | 3 (or 1 in dev) |
| Partitions | 256–512 per topic | 6–12 |
| Throughput | ~15 K msg/s sustained | ~50 msg/s |
| Retention | 3–7 d | 1 d |
| Alternative | — | Redis Streams is adequate below ~1 K msg/s |

---

## Failure modes

| Failure | Consequence | Mitigation |
| --- | --- | --- |
| Page bodies in Kafka | 108 TB/day of broker disk writes | Blob pointers only |
| Offsets committed before side effect | Silent data loss on consumer crash | Commit after |
| No lag-based backpressure | Retention expires; pages lost silently | Close the loop to crawl rate |
| Domain key skew | One partition saturated by a large site | Sub-partition high-volume domains only |
| Unclean leader election enabled | Committed messages silently truncated | Disable |
| Consumer rebalance storm | Throughput collapses under long processing | Raise `max.poll.interval.ms`; use cooperative sticky assignor |
| Retention too short for replay | Cannot recover from a parser bug | Retention ≥ time to detect + fix + reprocess |
