# HTML Parser

Turns fetched bytes into an indexable document: main text, title, fields, outbound links,
and the metadata the indexer needs.

Related: [TOKENIZATION](TOKENIZATION.md) · [CONTENT-DE-DUPLICATION](CONTENT-DE-DUPLICATION.md) · [INVERTED-INDEX](INVERTED-INDEX.md)

---

## Pipeline

```
raw bytes
  → charset detection      (header → meta → BOM → statistical fallback)
  → HTML parse             (error-tolerant, spec-compliant tree building)
  → [render decision]      (static parse, or headless browser)
  → boilerplate removal    (nav, footer, sidebar, cookie banner, ads)
  → main content extraction
  → language identification
  → field extraction       (title, headings, meta description, structured data)
  → link + anchor extraction
  → emit ParsedDocument
```

Use a **spec-compliant, error-tolerant** parser (`lxml`, `html5ever`, `jsoup`). Never regex.
Real-world HTML is broken in ways that will produce silently wrong text extraction, and
silently wrong text extraction is the worst class of bug in a search engine — it degrades
quality with no error signal.

---

## The rendering decision

A growing share of the web renders content client-side. Static parsing sees an empty
`<div id="root">`; a headless browser sees the article.

| | Static parse | Headless render |
| --- | --- | --- |
| Wall time | ~1 ms | ~600 ms |
| CPU | ~1 ms | ~1.2 CPU-seconds |
| Memory | negligible | ~300 MB |
| Ratio | 1× | **~500×** |

That sounds prohibitive. The arithmetic says otherwise:

```
25% of 400 M full fetches/day = 100 M renders/day = 1,160/s
1,160/s × 0.6 s = ~700 concurrent browsers
~1,400 cores ≈ 40 machines
```

Against a 27,000-host fleet, rendering is cheap. **Render more than instinct suggests.**

### Where this actually hurts

The classifier deciding *whether* to render is a major quality lever hiding inside an
operational detail. When it says no on a site that needed rendering, that site's content is
**silently absent from the index** — no error, no alert, just missing documents.

Instrument it as a quality surface with sampled human evaluation, not as a cost knob.

```
render if:
    body text after static parse < 200 chars, AND page has >20 KB of JS
  OR framework fingerprint detected (react-root, ng-app, __NUXT__, etc.)
  OR domain is on the render allow-list
  OR sampled 1% of everything  ← the audit path; without it you are blind
```

That last line is not optional. It is the only way to discover the classifier is wrong.

---

## Boilerplate removal

Roughly 70–80% of a typical page's text is navigation, footer, and chrome. Indexing it
pollutes every document with the same terms and destroys BM25's discriminative power.

Approaches, in increasing order of cost and quality:

| Method | How | Quality |
| --- | --- | --- |
| Tag heuristics | Drop `<nav>`, `<footer>`, `<aside>`, `<script>`, `<style>` | Poor alone, necessary first pass |
| Text-density | Score blocks by text:markup ratio, keep the dense run | Good — this is the workhorse |
| Site templates | Learn per-site repeated DOM subtrees across pages, subtract them | Best, needs multiple pages per site |
| Readability-style | Scoring heuristics on paragraph density and link density | Good general default |

Use text-density as the base and site-template subtraction where you have enough pages from
a domain. Template learning is where the real gains are, and it requires holding several
pages from the same site — an argument for grouping parse work by host.

---

## Field extraction

Fields get separate posting lists and separate weights in [BM25](BM25.md). Extract:

| Field | Source | Weight class |
| --- | --- | --- |
| `title` | `<title>`, `og:title`, `<h1>` fallback | Highest |
| `headings` | `<h1>`–`<h3>` | High |
| `body` | Main content after boilerplate removal | Base |
| `url_text` | Tokenised path segments | Medium |
| `meta_description` | `<meta name="description">` | Low — often spam |
| `anchors` | Filled in later, from *other* documents | Highest |
| `structured` | JSON-LD, microdata | Not scored; used for rich results |

**Anchor text is not extracted here.** It is discovered on *source* pages and must be
shuffled to *target* pages during indexing — see [DISTRIBUTED-INDEXING](DISTRIBUTED-INDEXING.md).
It is frequently the best description of a document, and it is the only way to index images,
PDFs, and pages whose own text is useless.

---

## Link extraction

```
for each <a href>:
    resolve against <base href> or document URL
    canonicalise                       (URL-DE-DUPLICATION.md)
    classify: internal / external / nofollow / ugc / sponsored
    capture anchor text + surrounding ±40 chars of context
    emit (source_url, target_url, anchor_text, rel_flags)
```

- Honour `rel="nofollow"` for authority flow ([PAGERANK](PAGERANK.md)) but still queue the
  URL for discovery — nofollow means "do not vouch", not "do not visit".
- Capture surrounding context, not just the anchor. "click here" is useless; the sentence
  around it is not.
- Cap links extracted per page (~3,000). A page with 50,000 links is a link farm or a bug.

---

## Language identification

Run on the extracted main text, not the raw HTML — markup skews n-gram statistics badly.
Use the `lang` attribute as a prior, never as truth; it is wrong often enough to matter.

Store per-document language and, for multilingual pages, per-block language. Tokenisation
([TOKENIZATION](TOKENIZATION.md)) branches on this, so an error here corrupts the index for
that document permanently.

---

## Target vs Build

| | Target | Build |
| --- | --- | --- |
| Parser | Custom C++/Rust, streaming | `lxml` / `selectolax` |
| Rendering | Own browser farm, 40 hosts | Playwright, allow-listed domains only |
| Boilerplate | Learned per-site templates | `trafilatura` / readability heuristics |
| Lang ID | Custom, per-block | `fasttext` / `langdetect` |
| Throughput | 10,000 docs/s | 100 docs/s |

---

## Failure modes

| Failure | Consequence | Mitigation |
| --- | --- | --- |
| Charset misdetection | Mojibake indexed as real terms | Header → meta → BOM → statistical, in that order. Validate with a decodability check. |
| Boilerplate not removed | Every doc on a site shares terms; BM25 discrimination collapses | Alert on per-site term-overlap ratio. |
| Main content removed as boilerplate | Document indexed with no content | Assert extracted text ≥ some fraction of visible text; sample and review. |
| Render classifier false negative | Site silently missing from index | The 1% sampled render audit path. |
| Parser hangs on hostile input | Worker thread lost | Hard timeout + memory cap per document, in a subprocess. |
| Zip/XML bomb | OOM | Cap decompressed size before parsing. |
| Render escapes sandbox | Compromised worker | Browser workers run unprivileged, network-restricted, no shared filesystem. Treat every rendered page as hostile. |
