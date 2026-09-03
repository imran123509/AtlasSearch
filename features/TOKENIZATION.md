# Tokenization

Turns text into the terms that go into the [inverted index](INVERTED-INDEX.md).

The one rule that matters: **the query must be tokenised exactly the same way as the
document.** Any asymmetry produces terms that can never match, and the failure is silent —
documents simply do not appear, with no error anywhere.

Related: [INVERTED-INDEX](INVERTED-INDEX.md) · [BM25](BM25.md) · [HTML-PARSER](HTML-PARSER.md)

---

## Pipeline

```
text
 → Unicode normalisation (NFKC)
 → case folding
 → script / language branch      ← everything below depends on this
 → segmentation
 → diacritic folding (language-dependent!)
 → stopword handling
 → stemming / lemmatisation
 → n-gram / shingle generation (optional)
 → terms
```

Language identification happens in the [parser](HTML-PARSER.md) and is stored per document.
An error there corrupts this document's terms permanently.

---

## Normalisation

**NFKC**, not NFC. NFKC additionally folds compatibility characters:

```
ﬁ (U+FB01 ligature)  → fi
①                    → 1
ｆｕｌｌｗｉｄｔｈ      → fullwidth
```

Without it, `ﬁle` and `file` are different terms and a user searching for one never finds
the other.

### Case folding

Use Unicode **full case folding**, not `str.lower()`. `ß` folds to `ss`; Turkish dotted/
dotless `İ`/`ı` need locale-aware handling. `.lower()` gets these wrong.

### Diacritics — language-dependent, and this matters

Folding `é → e` helps for English queries against French text. It **destroys meaning** in:

- **German**: `schön` ≠ `schon`, `Bär` ≠ `Bar`
- **Swedish/Finnish**: `ä`, `ö` are distinct letters, not decorated vowels
- **Spanish**: `año` ≠ `ano` — an embarrassing and well-known example

Branch on document language. Where language is uncertain, index **both** folded and unfolded
forms and let scoring prefer the exact match.

---

## Segmentation

| Script family | Approach |
| --- | --- |
| Latin, Cyrillic, Greek | Unicode word-break (UAX #29) + punctuation rules |
| Chinese, Japanese | Dictionary + statistical segmentation, **plus** character bigrams as a fallback |
| Korean | Morphological analysis; agglutinative, cannot be split on spaces |
| Thai, Khmer, Lao | Dictionary-based; no spaces between words |
| Arabic, Hebrew | Handle clitics and prefixes; optional vowel stripping |

For CJK, index **both** segmented words and character bigrams. Segmentation errors are
common and bigrams provide a recall floor when the segmenter gets it wrong.

### Things that must not be split

```
C++          → "c++"       not ["c"]
.NET         → ".net"      not ["net"]
0x80070643   → intact      ← users search for exactly these
COVID-19     → both "covid-19" and ["covid", "19"]
user@host    → both whole and parts
192.168.1.1  → intact
```

Rule: emit **both** the whole token and its parts, at the same position, so phrase queries
still work. Losing exact identifiers is one of the most visible quality failures a search
engine can have, and it is the thing dense retrieval is worst at ([RANKING](RANKING.md)).

---

## Stopwords

**Do not remove them at index time.** This was correct in 1998 when index size dominated;
it is wrong now.

Removing stopwords breaks:

```
"to be or not to be"     → nothing left
"The Who"                → nothing left
"let it be"              → "let"
"vitamin A"              → "vitamin"
```

Instead: index everything, and let **[BM25](BM25.md)'s IDF** handle it. A term appearing in
90% of documents contributes almost nothing to the score automatically — that is what IDF is
*for*. Block-max WAND then skips those postings cheaply ([INVERTED-INDEX](INVERTED-INDEX.md)).

At query time, treat high-DF terms as optional rather than required, so `the who` matches
documents containing both but does not fail when only one is present.

---

## Stemming

| Approach | Behaviour | Use |
| --- | --- | --- |
| Porter/Snowball | Rule-based suffix stripping. Aggressive. `university → univers`, `universe → univers` — a collision that damages precision. | Cheap default |
| Lemmatisation | Dictionary + POS. `better → good`, `ran → run`. Correct but slower. | Preferred |
| Both | Index the surface form **and** the stem at the same position | Chosen |

Indexing both lets scoring reward the exact surface match while stemmed matches still
contribute — recall from stemming, precision from the surface form. Costs ~30% more postings.

---

## Query-side symmetry

```python
# The only safe structure: one function, two callers.
def analyze(text: str, lang: str, *, query_side: bool = False) -> list[Token]:
    ...

index_terms = analyze(doc_text, doc_lang)
query_terms = analyze(query,   query_lang, query_side=True)
```

The `query_side` flag may **only** control behaviour that cannot break matching:

- ✅ synonym expansion (adds terms, never removes)
- ✅ spelling correction (before analysis)
- ❌ different normalisation, folding, stemming, or segmentation

**Any change to the analyzer requires a full index rebuild.** Analyzer changes and index
generations must be versioned together, and the version must be checked at query time. A
mismatched analyzer version is a silent, total quality failure.

---

## Target vs Build

| | Target | Build |
| --- | --- | --- |
| Implementation | Custom, per-language, SIMD | Lucene analyzers via OpenSearch |
| CJK | Own segmenter + bigram fallback | `analysis-kuromoji`, `analysis-smartcn` |
| Stemming | Lemmatiser + surface form | Snowball + `keyword_repeat` + `unique` |
| Throughput | 10,000 docs/s/host | 100 docs/s |

See [OPENSEARCH](OPENSEARCH.md) for the concrete analyzer definitions.

---

## Failure modes

| Failure | Consequence | Mitigation |
| --- | --- | --- |
| Index/query analyzer mismatch | Documents never match, silently, forever | One function, two callers. Version the analyzer with the index; assert on query. |
| Analyzer changed without rebuild | Partial index unreachable | Analyzer version in the generation manifest; refuse to serve on mismatch. |
| Diacritic folding on German/Swedish | Wrong results for a whole language | Branch on language; index both forms when uncertain. |
| Stopwords removed | Phrase queries and band names break | Do not remove. IDF handles it. |
| Identifier split | `0x80070643` unfindable | Emit whole token *and* parts at the same position. |
| Segmenter fails on CJK | Recall collapses for that language | Character bigram fallback indexed alongside. |
| `str.lower()` instead of case folding | `ß`, Turkish `İ` wrong | Use ICU full case folding. |
