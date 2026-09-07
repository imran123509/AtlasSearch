from __future__ import annotations

import random

import pytest

from atlas_indexer.index import (
    BM25,
    Index,
    InputDocument,
    SearchStats,
    search,
    search_exhaustive,
)
from atlas_indexer.index.dictionary import DictionaryWriter, TermInfo
from atlas_indexer.index.postings import NO_MORE, PostingsWriter
from atlas_indexer.index.segment import Segment, write_segment
from atlas_indexer.index.writer import IndexBuilder

VOCAB = ["black", "hole", "merger", "index", "block", "wand", "posting", "list",
         "score", "query", "term", "document", "the", "of", "and", "retrieval"]


def random_corpus(n_docs: int, seed: int = 0) -> list[InputDocument]:
    rng = random.Random(seed)
    docs = []
    for i in range(n_docs):
        length = rng.randint(5, 80)
        # Zipf-ish: a few terms are very common, most are rare.
        tokens = [
            VOCAB[min(int(rng.paretovariate(1.1)) - 1, len(VOCAB) - 1)]
            for _ in range(length)
        ]
        docs.append(
            InputDocument.from_tokens(f"doc-{i:04d}", tokens, static_rank=rng.random())
        )
    return docs


def assert_same_results(fast, slow, *, tol=1e-9):
    """Compare two result lists, tolerating tie permutation.

    `search` accumulates a document's score term-by-term as it walks; the
    exhaustive scorer accumulates per-term across the whole list. Float addition
    is not associative, so genuinely-equal scores can differ by ~1 ULP and two
    tied documents may come out in either order. That is not a retrieval
    difference, so the assertion checks what actually matters: the same
    documents, the same scores, and correct ordering.
    """
    assert [h.doc_id for h in fast].__len__() == len([h.doc_id for h in slow])
    assert {h.doc_id for h in fast} == {h.doc_id for h in slow}
    for a, b in zip(fast, slow):
        assert a.score == pytest.approx(b.score, abs=tol)
    scores = [h.score for h in fast]
    assert scores == sorted(scores, reverse=True), "results are not ranked"


@pytest.fixture
def corpus_index(tmp_path):
    idx = Index(tmp_path / "idx")
    idx.add_documents(random_corpus(600, seed=7))
    return idx


# ---------------------------------------------------------------------------
# The equivalence property
# ---------------------------------------------------------------------------

class TestBlockMaxWandCorrectness:
    """`search` must return exactly what `search_exhaustive` returns.

    An optimisation that quietly changes results is the worst kind of bug here:
    nothing in production reports it, the system just answers slightly worse
    forever. These are the tests that hold the whole design down.
    """

    @pytest.mark.parametrize("seed", range(8))
    def test_matches_exhaustive_on_random_corpora(self, tmp_path, seed):
        idx = Index(tmp_path / f"idx{seed}")
        idx.add_documents(random_corpus(400, seed=seed))
        segment = idx.segments[0]
        rng = random.Random(seed)

        for _ in range(15):
            terms = rng.sample(VOCAB, rng.randint(1, 4))
            k = rng.choice([1, 5, 10, 50])
            fast = search(segment, terms, k=k)
            slow = search_exhaustive(segment, terms, k=k)
            assert_same_results(fast, slow)

    @pytest.mark.parametrize("k", [1, 2, 3, 10, 100, 1000])
    def test_matches_exhaustive_at_every_k(self, corpus_index, k):
        segment = corpus_index.segments[0]
        terms = ["black", "merger", "index"]
        assert_same_results(search(segment, terms, k=k), search_exhaustive(segment, terms, k=k))

    def test_single_term_query(self, corpus_index):
        segment = corpus_index.segments[0]
        assert_same_results(
            search(segment, ["merger"], k=20), search_exhaustive(segment, ["merger"], k=20)
        )

    def test_absent_term_contributes_nothing_but_is_not_an_error(self, corpus_index):
        segment = corpus_index.segments[0]
        with_junk = search(segment, ["merger", "zzzznotaterm"], k=10)
        alone = search(segment, ["merger"], k=10)
        assert [h.doc_id for h in with_junk] == [h.doc_id for h in alone]

    def test_all_terms_absent(self, corpus_index):
        assert search(corpus_index.segments[0], ["nope", "alsonope"], k=10) == []

    def test_k_zero(self, corpus_index):
        assert search(corpus_index.segments[0], ["merger"], k=0) == []


# ---------------------------------------------------------------------------
# The safety property
# ---------------------------------------------------------------------------

class TestBlockMaxIsAnUpperBound:
    """Every stored block maximum must be >= the true score of every document
    in that block. A bound that is too low prunes a winner, silently."""

    def test_holds_for_every_term_and_block(self, corpus_index):
        segment = corpus_index.segments[0]
        scorer = segment.scorer
        avgdl = segment.stats.avg_doc_length
        checked = 0

        for term, _info in segment.terms.terms():
            cursor = segment.cursor(term)
            doc = cursor.doc()
            while doc != NO_MORE:
                actual = scorer.saturation(cursor.freq(), segment.doc_length(doc), avgdl)
                bound = cursor.block_max()
                assert bound >= actual - 1e-6, (
                    f"term={term!r} doc={doc}: bound {bound} < actual {actual}"
                )
                checked += 1
                doc = cursor.next_doc()

        assert checked > 1000, "test did not exercise enough postings to be meaningful"

    def test_f32_truncation_does_not_break_the_bound(self):
        """f64 maxima stored as f32 must round UP, never down."""
        w = PostingsWriter(block_size=4)
        # A value that is not exactly representable in float32.
        awkward = 1.7000000476837158203125 + 1e-12
        for i in range(4):
            w.add(i, 1, saturation=awkward)
        postings, _ = w.finish()
        from atlas_indexer.index.postings import PostingCursor

        cursor = PostingCursor(memoryview(postings), 0)
        assert cursor.block_max() >= awkward


class TestStaleMaximaBreakRetrieval:
    """Proof that the bound is load-bearing rather than decorative.

    Building a segment whose stored maxima are deliberately understated — which
    is exactly what a merge that copies old maxima produces — makes Block-Max
    WAND drop results the exhaustive scorer finds.
    """

    def _build_with_scaled_maxima(self, tmp_path, docs, scale: float) -> Segment:
        scorer = BM25()
        ordered = sorted(docs, key=lambda d: (-d.static_rank, d.external_id))
        from atlas_indexer.index.segment import DocEntry

        entries = [DocEntry(d.external_id, d.token_count, d.static_rank) for d in ordered]
        avgdl = sum(e.length for e in entries) / len(entries)

        inverted: dict[str, list[tuple[int, list[int]]]] = {}
        for doc_id, doc in enumerate(ordered):
            for term, positions in doc.terms.items():
                inverted.setdefault(term, []).append((doc_id, positions))

        dictionary = DictionaryWriter()
        postings_out = bytearray()
        positions_out = bytearray()
        for term in sorted(inverted):
            w = PostingsWriter()
            for doc_id, positions in inverted[term]:
                freq = len(positions)
                w.add(
                    doc_id, freq,
                    saturation=scorer.saturation(freq, entries[doc_id].length, avgdl) * scale,
                    positions=positions,
                )
            tp, tpos = w.finish()
            off = len(postings_out)
            postings_out.extend(tp)
            pos_off = len(positions_out)
            positions_out.extend(tpos)
            dictionary.add(term, TermInfo(w.doc_freq, w.total_tf, off, pos_off))

        write_segment(
            tmp_path, name="scaled", dictionary=dictionary,
            postings=bytes(postings_out), positions=bytes(positions_out),
            docs=entries, scorer=scorer,
        )
        return Segment(tmp_path)

    def test_understated_maxima_lose_results(self, tmp_path):
        docs = random_corpus(400, seed=3)
        bad = self._build_with_scaled_maxima(tmp_path / "bad", docs, scale=0.25)

        terms = ["black", "merger", "index"]
        pruned = search(bad, terms, k=10)
        truth = search_exhaustive(bad, terms, k=10)

        assert [h.doc_id for h in pruned] != [h.doc_id for h in truth], (
            "understated bounds should have caused wrong results — if this passes, "
            "the pruning is not actually using the stored maxima"
        )

    def test_correct_maxima_do_not(self, tmp_path):
        docs = random_corpus(400, seed=3)
        good = self._build_with_scaled_maxima(tmp_path / "good", docs, scale=1.0)
        terms = ["black", "merger", "index"]
        assert_same_results(search(good, terms, k=10), search_exhaustive(good, terms, k=10))

    def test_overstated_maxima_are_safe_just_slower(self, tmp_path):
        """A bound that is too HIGH costs work but never changes the answer."""
        docs = random_corpus(400, seed=3)
        loose = self._build_with_scaled_maxima(tmp_path / "loose", docs, scale=4.0)
        terms = ["black", "merger", "index"]
        assert_same_results(search(loose, terms, k=10), search_exhaustive(loose, terms, k=10))


# ---------------------------------------------------------------------------
# Pruning actually happens
# ---------------------------------------------------------------------------

class TestPruning:
    def test_blocks_are_skipped(self, tmp_path):
        idx = Index(tmp_path / "big")
        idx.add_documents(random_corpus(3000, seed=11))
        stats = SearchStats()
        search(idx.segments[0], ["black", "merger"], k=10, collect=stats)
        assert stats.blocks_skipped > 0, "no block was skipped — pruning is inert"

    def test_smaller_k_scores_fewer_candidates(self, tmp_path):
        """Retrieval cost is superlinear in k: theta rises faster with a smaller
        heap, so pruning bites harder. This is why reducing k is a cheap lever
        under load (FAILURE-HANDLING.md rung 2)."""
        idx = Index(tmp_path / "big")
        idx.add_documents(random_corpus(3000, seed=11))
        segment = idx.segments[0]

        small, large = SearchStats(), SearchStats()
        search(segment, ["black", "merger", "index"], k=10, collect=small)
        search(segment, ["black", "merger", "index"], k=1000, collect=large)
        assert small.candidates_scored < large.candidates_scored

    def test_a_rare_term_prunes_a_common_one(self, tmp_path):
        """A rare, high-IDF term keeps most of a common term's list unreachable.

        Note what this corpus does NOT show: every document here has the same
        frequency and a near-identical length, so every block maximum for
        "common" is effectively the same value and block-level skipping has no
        variation to exploit. The saving that does appear is WAND-level — the
        common cursor is advanced straight to the rare term's next document
        instead of being walked. `test_blocks_are_skipped` covers the
        block-level case on a corpus with real variation.
        """
        docs = []
        for i in range(2000):
            tokens = ["common"] * 5
            if i % 200 == 0:
                tokens.append("rare")
            docs.append(InputDocument.from_tokens(f"d{i}", tokens, static_rank=1 - i / 2000))
        idx = Index(tmp_path / "rare")
        idx.add_documents(docs)

        stats = SearchStats()
        hits = search(idx.segments[0], ["common", "rare"], k=5, collect=stats)
        assert hits
        assert stats.candidates_scored < 2000, "every document was scored — no pruning at all"
        assert_same_results(hits, search_exhaustive(idx.segments[0], ["common", "rare"], k=5))
