# Content De-duplication

Roughly **a third of the crawlable web is duplicate or near-duplicate**. Indexing it wastes
storage, wastes ranking capacity, and produces search results where positions 1–5 are the
same article on five domains.

Related: [URL-DE-DUPLICATION](URL-DE-DUPLICATION.md) · [HTML-PARSER](HTML-PARSER.md) · [RANKING](RANKING.md)

---

## Two problems, two mechanisms

| Problem | Example | Mechanism |
| --- | --- | --- |
| **Exact duplicates** | Mirrors, CDN copies, `http`/`https` pairs | Content hash |
| **Near-duplicates** | Syndicated news, scraped content, boilerplate variants, print views | SimHash over shingles |

Exact is cheap and catches maybe half of it. Near-duplicate detection is where the work is.

---

## Exact duplicates

```
content_hash = sha256(normalised_main_text)
```

Hash the **extracted main text**, not the raw bytes. Raw bytes differ on every fetch for any
page with a timestamp, a rotating ad slot, or a CSRF token — hashing them finds almost
nothing. Normalise whitespace and case first.

Doubles as the change-detection signal for [URL-FRONTIER](URL-FRONTIER.md) recrawl scheduling:
if `content_hash` is unchanged since last fetch, λ for this page drops.

---

## Near-duplicates: SimHash

A locality-sensitive hash where **similar documents produce hashes with small Hamming
distance** — unlike a cryptographic hash, where one changed character changes everything.

### Construction

```
1. shingle the text into overlapping w-grams (w = 5 words)
     "the quick brown fox jumps over" →
     {"the quick brown fox jumps", "quick brown fox jumps over"}

2. hash each shingle to 64 bits

3. build a 64-slot signed accumulator:
     for each shingle hash h, weighted by tf-idf:
         for bit i in 0..63:
             acc[i] += weight   if bit i of h is 1
             acc[i] -= weight   if bit i of h is 0

4. simhash bit i = 1 if acc[i] > 0 else 0
```

Two documents are near-duplicates if `hamming(a, b) ≤ 3` on 64 bits.

### Why shingles, not words

Bag-of-words says "the same 400 words in a different order" is identical. Shingles preserve
local ordering, which is exactly what distinguishes a rewrite from a copy.

### Finding candidates without O(n²)

Comparing 10¹⁰ documents pairwise is impossible. Standard trick: **banding**.

```
split the 64-bit simhash into 4 bands of 16 bits
index each document under all 4 band values
two docs within Hamming distance 3 must share at least one band exactly
  (pigeonhole: 3 differing bits cannot touch all 4 bands)
→ candidates = documents sharing any band
→ verify exact Hamming distance only on candidates
```

Turns a quadratic problem into four hash-table lookups per document.

---

## Canonical election

Once a near-duplicate cluster is found, exactly one member gets indexed. The others become
aliases pointing at it.

```
score = w₁·authority        (PageRank of the URL — see PAGERANK.md)
      + w₂·first_seen       earliest crawl date wins; originals beat scrapers
      + w₃·content_length   more complete version wins
      + w₄·host_trust
      + w₅·is_https
      − w₆·ad_density
      − w₇·boilerplate_ratio
```

`first_seen` is the strongest single signal against scrapers, and the reason to store crawl
timestamps permanently even though nothing else needs them.

### Cluster stability matters more than cluster correctness

If the canonical flips between two members on successive index builds, the URL in search
results changes, links break, and rank history resets. Add hysteresis: a challenger must beat
the incumbent by a margin, not merely tie.

```
if score(challenger) > score(incumbent) × 1.15:
    promote
```

---

## Where this runs

Near-duplicate clustering is a **global** operation — it needs to compare documents that live
on different crawl shards. It runs during index build, not during crawl:

```
parsed docs → simhash + bands → shuffle by band value
            → cluster within band → elect canonical → emit alias map
            → indexer indexes canonicals only
```

See [DISTRIBUTED-INDEXING](DISTRIBUTED-INDEXING.md) for the shuffle.

---

## Result-time diversity is a separate problem

Even a perfectly de-duplicated index produces SERPs where positions 1–5 are five different
articles about the same event from five outlets. That is not duplication — those are distinct
documents. It is a **ranking diversity** problem, handled at L2:

```
cap results per registrable domain (typically 2)
cluster the result set by topic and interleave clusters
```

Do not try to solve it with a lower near-duplicate threshold. Lowering the threshold until
distinct articles collapse destroys real content and is very hard to undo — the alias map is
applied at index build.

---

## Target vs Build

| | Target | Build |
| --- | --- | --- |
| Exact | SHA-256 of main text | Same |
| Near-dup | 64-bit SimHash, 4-band index, global shuffle | Same algorithm, single-node |
| Cluster scale | 10¹⁰ docs | 10⁷ docs |
| Where | Index build shuffle stage | In the indexer process |

---

## Failure modes

| Failure | Consequence | Mitigation |
| --- | --- | --- |
| Threshold too loose | Distinct articles merged; content lost from the index | Hold out a labelled pair set; measure precision/recall on it before changing the threshold. |
| Threshold too tight | Scraped copies all indexed; SERP fills with duplicates | Same measurement, other direction. |
| Canonical flip-flop | Result URLs change between builds, links break | Hysteresis margin on promotion. |
| Scraper wins election | The copy outranks the original | Weight `first_seen` heavily. This is the whole ballgame for news. |
| Hashing raw bytes | Near-zero exact-duplicate detection | Hash normalised extracted text. |
| Boilerplate dominates the shingles | Every page on a site looks like a near-duplicate | Run after boilerplate removal, never before. |
