# indexer — parse stage

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

## Modules

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
pytest -q          # 207 tests, no network, no Kafka, no S3
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

## Environment

| Variable | Default | Notes |
| --- | --- | --- |
| `PARSE_MAX_BYTES` | `10485760` | Cap enforced during decompression |
| `PARSE_MAX_NODES` | `500000` | A half-million-node document is not prose |
| `PARSE_MAX_LINKS` | `3000` | Above this it is a link farm or a bug |
| `PARSE_TIMEOUT` | `10.0` | Seam for subprocess isolation |
| `RENDER_AUDIT_RATE` | `0.01` | **Do not set to 0 in production** |
| `KAFKA_BROKERS` / `S3_ENDPOINT` / `S3_BUCKET` | localhost | |
