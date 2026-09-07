"""BM25 scoring, split so that block maxima stay valid.

The whole block-max design rests on a precomputed per-block **upper bound**. A
bound that is too low silently prunes documents that should have won — no error,
no alert, just worse results. So it matters enormously which parts of the score
are baked into that bound and which are applied at query time.

BM25 factorises cleanly:

    score(t,d) = IDF(t)  x  saturation(tf, |d|)

                             f(t,d) . (k1 + 1)
    saturation = -------------------------------------------
                 f(t,d) + k1 . (1 - b + b . |d| / avgdl)

Only the right-hand factor depends on the document. We store block maxima of
**saturation alone** and multiply IDF in at query time, which means:

  * `N` and `df` can change freely — merges, deletions, a new segment — without
    invalidating a single stored bound.
  * `avgdl` cannot. It appears inside the saturation term, and it *rises* when
    long documents are merged in, which *raises* saturation and turns a stored
    bound into an underestimate. Merges must therefore recompute block maxima;
    see merge.py and the monotonicity tests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

K1_DEFAULT = 1.2
B_DEFAULT = 0.75


@dataclass(frozen=True, slots=True)
class CorpusStats:
    """Collection-level statistics needed to score.

    Held separately from the segment so a multi-segment index can score with
    *global* stats rather than shard-local ones — the inconsistency
    features/BM25.md warns about when shards are non-uniform.
    """

    doc_count: int
    avg_doc_length: float

    def merged_with(self, other: CorpusStats) -> CorpusStats:
        total = self.doc_count + other.doc_count
        if total == 0:
            return CorpusStats(0, 0.0)
        avg = (
            self.avg_doc_length * self.doc_count + other.avg_doc_length * other.doc_count
        ) / total
        return CorpusStats(total, avg)


@dataclass(frozen=True, slots=True)
class BM25:
    k1: float = K1_DEFAULT
    b: float = B_DEFAULT

    def idf(self, doc_freq: int, doc_count: int) -> float:
        """Robertson-Sparck-Jones IDF with the +1 that keeps it non-negative.

        Without the +1, a term in more than half the corpus gets negative weight
        and a document is *penalised* for containing "the".
        """
        if doc_count <= 0:
            return 0.0
        return math.log(1.0 + (doc_count - doc_freq + 0.5) / (doc_freq + 0.5))

    def saturation(self, tf: int, doc_length: int, avg_doc_length: float) -> float:
        """The document-dependent factor. This is what block maxima bound."""
        if tf <= 0:
            return 0.0
        avg = avg_doc_length if avg_doc_length > 0 else 1.0
        norm = self.k1 * (1.0 - self.b + self.b * doc_length / avg)
        return tf * (self.k1 + 1.0) / (tf + norm)

    def score(self, tf: int, doc_length: int, doc_freq: int, stats: CorpusStats) -> float:
        return self.idf(doc_freq, stats.doc_count) * self.saturation(
            tf, doc_length, stats.avg_doc_length
        )

    def max_saturation(self, avg_doc_length: float) -> float:
        """Supremum of `saturation` over every possible (tf, length).

        Saturation is increasing in tf and decreasing in length, so the limit is
        tf -> infinity with length -> 0, giving exactly (k1 + 1). Used as the
        fallback bound when a real one is unavailable — always safe, never tight.
        """
        return self.k1 + 1.0

    def upper_bound_for(self, max_tf: int, min_length: int, avg_doc_length: float) -> float:
        """Tightest safe bound given the extremes actually present in a block."""
        return self.saturation(max_tf, max(min_length, 1), avg_doc_length)
