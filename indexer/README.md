# indexer — parse stage + inverted index

Implementation of [features/HTML-PARSER.md](../features/HTML-PARSER.md).

Consumes `pages.fetched`, produces `pages.parsed`, `links.extracted`, `urls.discovered`.

```
raw bytes
  → decompress + size cap        bomb defence, before anything reads the bytes
  → charset detection            BOM → header → meta → statistical
  → HTML5 tree build             lexbor, node/depth capped
  → boilerplate removal          structural → density → site template
  → render decision              static, or hand to the browser pool
  → language identification      on extracted text, never on markup
  → field extraction             title / headings / meta / JSON-LD / canonical
  → link + anchor extraction     base href, rel classification, ±40 char context
  → ParsedDocument
```

## Two packages live here

| Package | Implements | Entry point |
| --- | --- | --- |
| `atlas_indexer` | [HTML-PARSER.md](../features/HTML-PARSER.md) — the parse stage | `Parser`, `ParseWorker` |
| `atlas_indexer.index` | [INVERTED-INDEX.md](../features/INVERTED-INDEX.md) — the block-max index | `Index`, `InputDocument` |

## Parse modules

| Module | Responsibility |
| --- | --- |
| `charset.py` | Encoding cascade, HTML5 alias table, declaration validation |
| `parse.py` | Bounded tree building; decompression, node and size caps |
| `boilerplate.py` | Three-layer content extraction + retention diagnostics |
| `fields.py` | Title cascade, headings, meta, JSON-LD, robots meta, URL tokenisation |
| `links.py` | Resolution, canonicalisation, rel classification, anchor context |
| `language.py` | Language ID with the `lang` attribute as prior, never truth |
| `render.py` | Render decision + the sampled audit path |
| `pipeline.py` | Orchestration → `ParsedDocument` |
| `main.py` | Kafka worker, blob reader, emission |

## Run it

```bash
python -m venv .venv && . .venv/Scripts/activate
pip install selectolax charset-normalizer py3langid structlog prometheus-client tldextract

# Parse a local file and print what was extracted — the fastest way to eyeball
# whether the boilerplate remover is behaving on a real page.
python -m atlas_indexer.main --file page.html --url https://example.com/page

# Consume from Kafka, reading bodies from local blobs
python -m atlas_indexer.main --blob-dir ../crawler/blobs --metrics-port 9101
```

## Test

```bash
pytest -q          # 347 tests, no network, no Kafka, no S3
```

## Three bugs the tests caught

**The decompression-bomb defence was dead code on the real path.** `pipeline.parse()`
decoded the charset first and then handed pre-decoded text to `safe_parse`, which
skips `decompress_capped` when given `text=`. So a gzipped body was never bomb-checked
*and* was charset-detected while still compressed, producing mojibake. Order is now
decompress → detect → decode → parse.

**Missing DOM neighbours voted for boilerplate.** The Kohlschütter classifier keys on
the word counts of the previous and next blocks. At a document boundary those are
*undefined*, and treating them as zero makes every `<= 4` / `<= 17` branch fire — so a
short article with navigation above it and nothing below was dropped in full. A
sentinel now abstains instead of voting.

**The `no_body` check could never fire.** lexbor synthesises a `<body>` for any input,
including an RSS feed, so "no body" cannot detect non-HTML. The check is kept for
genuinely degenerate trees and the comment no longer claims more than it does;
non-HTML is filtered by content-type upstream and by `is_indexable` downstream.

## Two deliberate deviations

**Charset ordering.** The doc specifies `header → meta → BOM → statistical`. That puts
the BOM third, which is wrong: HTML5 treats a BOM as authoritative and overriding,
because a BOM is a fact about the bytes while a declaration is a claim about them. A
page served `charset=iso-8859-1` that starts with a UTF-8 BOM is UTF-8. Implemented as
`BOM → header → meta → statistical`, and declared encodings are validated by trial
decode before being trusted.

**`iso-8859-1` is decoded as `windows-1252`.** HTML5 mandates it, and it is not
pedantry — a large share of the web declares latin-1 while emitting cp1252 bytes, so
honouring the declaration literally turns every curly apostrophe into `�`.

## Diagnostics are not debug output

`ParsedDocument` carries `retained_ratio`, `blocks_kept/blocks_total`, and `warnings`,
and `metrics.py` exports their distributions. These exist because the worst failure
this stage can have is **silent**: if the boilerplate remover starts eating main
content after a deploy, documents index fine, queries succeed, latency is normal, and
result quality quietly collapses. `parse_extracted_text_length` dropping is the only
signal. Do not slim them out of the payload.

Same reasoning for `render.audit_sample_rate`. A render-classifier false negative
produces no error — just a site missing from the index. The 1% sample is the only way
to discover the rule set is wrong, and `parse_render_audit_disagreements_total` is
where that shows up.

## Known structural problem

`links.py` imports `canonicalise` from `atlas_crawler.urlnorm`. Sharing is correct —
if the crawler and indexer canonicalise differently, one URL gets two fingerprints and
de-duplication silently fails — but the dependency direction is wrong. It should be a
`common/` package that both services depend on.

Importing it originally dragged httpx, redis and the whole fetch stack into this
service; the crawler's `__init__.py` is now lazy (PEP 562) so `atlas_crawler.urlnorm`
costs only `tldextract`. That is a mitigation, not the fix.

## Not implemented here

- **Rendering itself.** `render.decide()` returns the decision; a separate
  memory-bound browser pool acts on it. It executes untrusted web content, so it
  belongs in its own namespace with an egress NetworkPolicy —
  see [KUBERNETES](../features/KUBERNETES.md).
- **Anchor text on the receiving side.** Anchors are discovered here on *source* pages
  and must be shuffled to *target* pages at index build; a parser cannot know a
  document's anchors because they live on other documents.
  See [DISTRIBUTED-INDEXING](../features/DISTRIBUTED-INDEXING.md).
- **Content de-duplication.** Needs a global shuffle by SimHash band.
  See [CONTENT-DE-DUPLICATION](../features/CONTENT-DE-DUPLICATION.md).
- **Subprocess isolation.** In-process caps bound size, node count and depth; they
  cannot bound wall time or RSS. `parse.parse_in_subprocess` is the documented seam
  and is not wired up.

## The inverted index (`atlas_indexer.index`)

**This is the Target artifact, not the Build.** INVERTED-INDEX.md is explicit
that the Build should use Lucene via OpenSearch and not hand-roll an index
format — and that guidance still holds. What lives here is the format the Build
migrates *to*, once per-document JVM overhead and the absence of a tiering
primitive stop being acceptable, plus a reference implementation that makes the
mechanics directly testable.

| Module | Responsibility |
| --- | --- |
| `codec.py` | varint, d-gaps, bitpacking, f32-safe rounding |
| `dictionary.py` | Front-coded term dictionary, binary-searched |
| `postings.py` | Block-max posting lists + skip table; the cursor |
| `scoring.py` | BM25, split so block maxima survive IDF changes |
| `wand.py` | Block-Max WAND, plus the exhaustive reference scorer |
| `deletes.py` | Tombstones, applied at query time |
| `segment.py` | Immutable segment: manifest, checksums, forward index |
| `merge.py` | LSM compaction — **recomputes block maxima** |
| `writer.py` | docID assignment in static-rank order |
| `index.py` | Multi-segment search, generations |

```python
from atlas_indexer.index import Index, InputDocument

index = Index(path)
index.add_documents([
    InputDocument.from_tokens("doc-1", ["block", "max", "wand"], static_rank=0.9),
])
index.publish("gen-20260908-0600")
index.search(["block", "wand"], k=10)
```

### Measured on a 20k-document corpus

| | Achieved | Doc's estimate |
| --- | --- | --- |
| postings.bin | **1.10 B/posting** | ~1.3 |
| positions.bin | 1.18 B/position | ~0.9 |
| terms.dict | 12.9 B/term | — |
| Total vs. fixed-width | **25.9%** | — |

Positions come in worse than estimated because a synthetic corpus has no
locality; real documents produce smaller position deltas.

### Two bugs the equivalence test caught

`search` must return exactly what `search_exhaustive` returns. That property is
what these tests exist for, and it found two genuine algorithm bugs that would
have degraded results silently in production:

**Inspecting a block moved the cursor.** `advance_block` advanced the *document*
pointer as a side effect, but Block-Max WAND checks bounds for terms still
positioned behind the pivot — so every posting between their position and the
pivot was skipped. Bound inspection is now `block_max_at()`, a pure read.

**The pivot prefix excluded tied terms.** The cumulative-bound scan stops at the
first term that pushes the sum past theta, but ties are adjacent in the sorted
order, so other terms could sit on the *same document* with their bounds
uncounted. `block_sum` was an underestimate, and an underestimated bound skips a
real winner. Found via a document matching three of four query terms where only
two were counted.

A third, smaller one: block maxima are computed in f64 and stored as f32, and
plain truncation rounds **down** — turning a true upper bound into a value
fractionally below the real maximum. `round_up_f32` nudges to the next
representable value.

### Why the bound is load-bearing

`TestStaleMaximaBreakRetrieval` builds segments with maxima scaled by 0.25x,
1.0x and 4.0x, and asserts that understated bounds *do* lose results while
overstated ones cost work but never change the answer. That is the failure
INVERTED-INDEX.md singles out — "skip data stale after merge -> wrong results,
silently" — and it is why `merge.py` recomputes rather than copies.

The mechanism: block maxima bound BM25's saturation term, which contains
`|d| / avgdl`. Merging changes `avgdl`, and if it *rises* the denominator shrinks,
saturation rises, and a copied maximum becomes an underestimate. Scoring is
factorised so IDF is applied at query time — meaning `N` and `df` can change
freely without invalidating a bound — but `avgdl` cannot be factored out.

### Static rank is the docID order

`IndexBuilder` sorts by descending static rank before assigning docIDs. Posting
lists are in docID order for d-gap compression anyway, so making docID order
*also* quality order means walking a list forward walks best-to-worst, theta
rises fast, and pruning bites early.

It is also exactly why re-scoring the corpus is a full rebuild: changing the
static-rank formula changes the docID assignment, invalidating every posting
list, every d-gap and every block maximum. `test_merge_preserves_static_rank_order`
holds this down.

## Environment

| Variable | Default | Notes |
| --- | --- | --- |
| `PARSE_MAX_BYTES` | `10485760` | Cap enforced during decompression |
| `PARSE_MAX_NODES` | `500000` | A half-million-node document is not prose |
| `PARSE_MAX_LINKS` | `3000` | Above this it is a link farm or a bug |
| `PARSE_TIMEOUT` | `10.0` | Seam for subprocess isolation |
| `RENDER_AUDIT_RATE` | `0.01` | **Do not set to 0 in production** |
| `KAFKA_BROKERS` / `S3_ENDPOINT` / `S3_BUCKET` | localhost | |
