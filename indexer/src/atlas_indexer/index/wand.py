"""Block-Max WAND — top-k retrieval with early termination.

The loop keeps a threshold **theta** equal to the score of the current k-th best
result. For each candidate alignment it sums the per-term block maxima; if that
sum cannot exceed theta, the whole block is skipped **without decoding a single
posting**.

    query: "black merger"     theta = 5.0

    block:        0     1     2     3     4     5     6     7
    black  max: 1.9   2.1   1.8   2.2   2.0   1.7   2.1   1.9
    merger max: 6.2     -     -   5.9     -   6.4     -   5.5
                ----  ----  ----  ----  ----  ----  ----  ----
    sum:        8.1   2.1   1.8   8.1   2.0   8.1   2.1   7.4
                ok    skip  skip  ok    skip  ok    skip  ok

The feedback matters more than the skipping: every good document found raises
theta, which prunes harder. That is why top-10 is far cheaper than top-1000, and
why lowering k is a cheap lever under load (features/FAILURE-HANDLING.md, rung 2).

`search_exhaustive` scores every candidate with no pruning. It exists to be
compared against `search` in tests: an optimisation that returns different
results from the naive path is a bug, and this is the only way to know.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass

from .postings import NO_MORE, PostingCursor
from .scoring import BM25, CorpusStats


@dataclass(slots=True)
class Hit:
    doc_id: int
    score: float
    external_id: str = ""

    def __lt__(self, other: Hit) -> bool:  # for heap ordering on ties
        return (self.score, -self.doc_id) < (other.score, -other.doc_id)


@dataclass(slots=True)
class SearchStats:
    """Instrumentation, so the pruning can be shown to actually happen."""

    candidates_scored: int = 0
    blocks_skipped: int = 0
    blocks_entered: int = 0
    postings_decoded: int = 0

    @property
    def skip_ratio(self) -> float:
        total = self.blocks_skipped + self.blocks_entered
        return self.blocks_skipped / total if total else 0.0


class _Term:
    __slots__ = ("term", "cursor", "idf", "max_score")

    def __init__(self, term: str, cursor: PostingCursor, idf: float) -> None:
        self.term = term
        self.cursor = cursor
        self.idf = idf
        # Global upper bound: IDF times the largest block maximum anywhere in
        # this term's list. Used for pivot selection before block refinement.
        self.max_score = idf * max((s.max_saturation for s in cursor.skips), default=0.0)

    def block_max_score_at(self, target: int) -> float:
        """Bound for the block containing `target`. Must not move the cursor."""
        return self.idf * self.cursor.block_max_at(target)

    def doc(self) -> int:
        return self.cursor.doc()


def _open_terms(segment, terms: list[str], stats: CorpusStats) -> list[_Term]:
    scorer = segment.scorer
    out: list[_Term] = []
    for term in terms:
        cursor = segment.cursor(term)
        if cursor is None:
            continue  # absent term contributes nothing; not an error
        info = segment.term_info(term)
        idf = scorer.idf(info.doc_freq, stats.doc_count)
        out.append(_Term(term, cursor, idf))
    return out


def search(
    segment,
    terms: list[str],
    *,
    k: int = 10,
    stats: CorpusStats | None = None,
    collect: SearchStats | None = None,
) -> list[Hit]:
    """Top-k over the union of `terms`, with block-max pruning."""
    stats = stats or segment.stats
    scorer: BM25 = segment.scorer
    avgdl = stats.avg_doc_length

    active = _open_terms(segment, terms, stats)
    if not active or k <= 0:
        return []

    heap: list[Hit] = []
    theta = 0.0

    while True:
        active = [t for t in active if t.doc() != NO_MORE]
        if not active:
            break
        active.sort(key=lambda t: t.doc())

        # --- pivot selection on global bounds ------------------------------
        acc = 0.0
        pivot = -1
        for i, t in enumerate(active):
            acc += t.max_score
            if acc > theta:
                pivot = i
                break
        if pivot < 0:
            break  # no remaining document can beat the threshold

        pivot_doc = active[pivot].doc()

        # Extend the prefix over every term sitting on the pivot document.
        # The cumulative-bound scan stops at the *first* term that pushes the
        # sum past theta, but ties are adjacent in the sorted order, so terms
        # after it may share pivot_doc. Leaving them out makes `block_sum` an
        # underestimate of what this document can score — and an underestimated
        # bound skips a real winner, silently.
        last = pivot
        while last + 1 < len(active) and active[last + 1].doc() == pivot_doc:
            last += 1
        prefix = active[: last + 1]

        # --- block-max refinement -------------------------------------------
        # Read-only: these terms are still positioned behind the pivot, and
        # moving their cursors here would drop the postings in between.
        block_sum = sum(t.block_max_score_at(pivot_doc) for t in prefix)

        if block_sum <= theta:
            # Nothing in this block alignment can win. Jump past the earliest
            # block end, decoding nothing.
            if collect:
                collect.blocks_skipped += len(prefix)
            # How far the bound stays valid. Two ceilings:
            #   * the earliest block end — past it a prefix term enters a new
            #     block whose maximum may be higher;
            #   * the next document held by a term OUTSIDE the prefix — from
            #     there on, a term whose bound was never counted starts
            #     contributing, so block_sum no longer bounds anything.
            limit = min(
                (t.cursor.block_last_at(pivot_doc) for t in prefix), default=pivot_doc
            )
            if last + 1 < len(active):
                next_outside = active[last + 1].doc()
                if next_outside != NO_MORE:
                    limit = min(limit, next_outside - 1)
            target = max(limit + 1, pivot_doc + 1)
            for t in prefix:
                if t.doc() < target:
                    t.cursor.advance(target)
            continue

        if collect:
            collect.blocks_entered += len(prefix)

        if active[0].doc() == pivot_doc:
            # Every term in the prefix is aligned on pivot_doc: score it.
            if not segment.is_deleted(pivot_doc):
                doc_length = segment.doc_length(pivot_doc)
                score = 0.0
                for t in active:
                    if t.doc() == pivot_doc:
                        score += t.idf * scorer.saturation(
                            t.cursor.freq(), doc_length, avgdl
                        )
                        if collect:
                            collect.postings_decoded += 1
                if collect:
                    collect.candidates_scored += 1

                if len(heap) < k:
                    heapq.heappush(heap, Hit(pivot_doc, score))
                    if len(heap) == k:
                        theta = heap[0].score
                elif score > theta:
                    heapq.heapreplace(heap, Hit(pivot_doc, score))
                    theta = heap[0].score

            for t in active:
                if t.doc() == pivot_doc:
                    t.cursor.next_doc()
        else:
            # Pull the lagging terms up to the pivot.
            for t in active[:pivot]:
                if t.doc() < pivot_doc:
                    t.cursor.advance(pivot_doc)

    results = sorted(heap, key=lambda h: (-h.score, h.doc_id))
    for hit in results:
        hit.external_id = segment.external_id(hit.doc_id)
    return results


def search_exhaustive(
    segment, terms: list[str], *, k: int = 10, stats: CorpusStats | None = None
) -> list[Hit]:
    """Score every matching document with no pruning whatsoever.

    The reference implementation. `search` must agree with this exactly — an
    optimisation that quietly changes results is the worst kind of bug, because
    nothing in production would report it.
    """
    stats = stats or segment.stats
    scorer: BM25 = segment.scorer
    avgdl = stats.avg_doc_length

    totals: dict[int, float] = {}
    for term in terms:
        cursor = segment.cursor(term)
        if cursor is None:
            continue
        idf = scorer.idf(segment.term_info(term).doc_freq, stats.doc_count)
        doc = cursor.doc()
        while doc != NO_MORE:
            if not segment.is_deleted(doc):
                totals[doc] = totals.get(doc, 0.0) + idf * scorer.saturation(
                    cursor.freq(), segment.doc_length(doc), avgdl
                )
            doc = cursor.next_doc()

    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
    return [Hit(doc_id, score, segment.external_id(doc_id)) for doc_id, score in ranked]
