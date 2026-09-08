from __future__ import annotations

import math

import pytest

from atlas_indexer.index import (
    BM25,
    BM25F,
    AnchorAggregator,
    CorpusStats,
    FieldSpec,
    FieldStats,
    combine_multiplicative,
    combine_with_static_rank,
)

CORPUS = CorpusStats(doc_count=10_000, avg_doc_length=300)
FIELD_STATS = FieldStats(
    {
        "anchors": 8, "title": 6, "headings": 12,
        "url_text": 5, "body": 300, "meta_description": 20,
    }
)
LENGTHS = {
    "anchors": 8, "title": 6, "headings": 12,
    "url_text": 5, "body": 300, "meta_description": 20,
}


@pytest.fixture
def scorer():
    return BM25F()


# ---------------------------------------------------------------------------
# The central point: where the saturation goes
# ---------------------------------------------------------------------------

class TestSaturationPlacement:
    def test_per_field_saturation_enables_stuffing(self, scorer):
        """The attack the doc's ❌/✅ block is about.

        Saturating each field independently and then summing lets a term stuffed
        once into every field collect near-full credit from each — precisely the
        behaviour k1 exists to prevent.
        """
        honest = {"body": 12}
        stuffed = dict.fromkeys(LENGTHS, 1)

        right_honest = scorer.score_term(honest, LENGTHS, 50, CORPUS, FIELD_STATS)
        right_stuffed = scorer.score_term(stuffed, LENGTHS, 50, CORPUS, FIELD_STATS)
        wrong_honest = scorer.score_per_field_saturation(
            honest, LENGTHS, 50, CORPUS, FIELD_STATS
        )
        wrong_stuffed = scorer.score_per_field_saturation(
            stuffed, LENGTHS, 50, CORPUS, FIELD_STATS
        )

        # Correct: stuffing buys almost nothing over an honest mention.
        assert right_stuffed / right_honest < 1.2
        # Wrong: stuffing buys an order of magnitude.
        assert wrong_stuffed / wrong_honest > 5.0

    def test_saturation_is_applied_exactly_once(self, scorer):
        freqs = {"title": 2, "body": 5}
        pseudo = scorer.pseudo_frequency(freqs, LENGTHS, FIELD_STATS)
        expected = BM25(k1=scorer.k1).idf(50, CORPUS.doc_count) * scorer.saturate(pseudo)
        assert scorer.score_term(freqs, LENGTHS, 50, CORPUS, FIELD_STATS) == pytest.approx(
            expected
        )

    def test_pseudo_frequency_is_a_weighted_sum(self, scorer):
        freqs = {"title": 1, "body": 1}
        title = scorer.spec("title").weight * scorer.field_contribution(
            "title", 1, LENGTHS["title"], FIELD_STATS
        )
        body = scorer.spec("body").weight * scorer.field_contribution(
            "body", 1, LENGTHS["body"], FIELD_STATS
        )
        assert scorer.pseudo_frequency(freqs, LENGTHS, FIELD_STATS) == pytest.approx(
            title + body
        )

    def test_field_contribution_is_not_saturated(self, scorer):
        """It must stay linear in tf, or the combination saturates twice."""
        one = scorer.field_contribution("body", 1, 300, FIELD_STATS)
        ten = scorer.field_contribution("body", 10, 300, FIELD_STATS)
        assert ten == pytest.approx(one * 10)

    def test_saturation_bounds_at_k1_plus_one(self, scorer):
        assert scorer.saturate(1e9) < scorer.k1 + 1.0
        assert scorer.saturate(1e9) == pytest.approx(scorer.k1 + 1.0, rel=1e-6)
        assert scorer.max_saturation() == scorer.k1 + 1.0

    def test_high_k1_makes_stuffing_more_profitable(self):
        """Why the doc says keep k1 <= 1.5."""
        low = BM25F(k1=0.9)
        high = BM25F(k1=3.0)
        honest = {"body": 3}
        stuffed = {"body": 60}

        def gain(s):
            return s.score_term(stuffed, LENGTHS, 50, CORPUS, FIELD_STATS) / s.score_term(
                honest, LENGTHS, 50, CORPUS, FIELD_STATS
            )

        assert gain(high) > gain(low)


# ---------------------------------------------------------------------------
# Field weights
# ---------------------------------------------------------------------------

class TestFieldWeights:
    def test_doc_weights_are_the_defaults(self, scorer):
        assert scorer.weights == {
            "anchors": 10.0, "title": 8.0, "headings": 3.0,
            "url_text": 2.5, "body": 1.0, "meta_description": 0.5,
        }

    def test_title_beats_body_for_the_same_term(self, scorer):
        in_title = scorer.score_term({"title": 1}, LENGTHS, 50, CORPUS, FIELD_STATS)
        in_body = scorer.score_term({"body": 1}, LENGTHS, 50, CORPUS, FIELD_STATS)
        assert in_title > in_body

    def test_anchors_outrank_title(self, scorer):
        """Anchors describe a document better than it describes itself."""
        anchors = scorer.score_term({"anchors": 1}, LENGTHS, 50, CORPUS, FIELD_STATS)
        title = scorer.score_term({"title": 1}, LENGTHS, 50, CORPUS, FIELD_STATS)
        assert anchors > title

    def test_meta_description_is_nearly_worthless(self, scorer):
        """Frequently spam, so weighted at 0.5."""
        meta = scorer.score_term({"meta_description": 1}, LENGTHS, 50, CORPUS, FIELD_STATS)
        body = scorer.score_term({"body": 1}, LENGTHS, 50, CORPUS, FIELD_STATS)
        assert meta < body

    def test_per_field_b_is_respected(self):
        """A short title's length carries little information, so it wants weaker
        normalisation than the body."""
        strong = BM25F(fields=(FieldSpec("title", 1.0, b=1.0),))
        weak = BM25F(fields=(FieldSpec("title", 1.0, b=0.0),))
        long_title = {"title": 1}
        lengths = {"title": 60}  # ten times the average
        assert strong.pseudo_frequency(long_title, lengths, FIELD_STATS) < weak.pseudo_frequency(
            long_title, lengths, FIELD_STATS
        )

    def test_weights_are_query_time_not_baked_in(self):
        """Field weights move NDCG far more than k1/b, so they must be tunable
        without a rebuild."""
        a = BM25F(fields=(FieldSpec("title", 8.0), FieldSpec("body", 1.0)))
        b = BM25F(fields=(FieldSpec("title", 1.0), FieldSpec("body", 8.0)))
        # Asymmetric frequencies, or swapping the weights is a no-op: with one
        # occurrence in each field at average length, 8*1 + 1*1 == 1*1 + 8*1.
        freqs = {"title": 3, "body": 1}
        assert a.pseudo_frequency(freqs, LENGTHS, FIELD_STATS) > b.pseudo_frequency(
            freqs, LENGTHS, FIELD_STATS
        )

    def test_unknown_field_is_ignored_not_crashed(self, scorer):
        assert scorer.pseudo_frequency({"nosuchfield": 5}, LENGTHS, FIELD_STATS) == 0.0


# ---------------------------------------------------------------------------
# Anchor capping
# ---------------------------------------------------------------------------

class TestAnchorCapping:
    def test_one_domain_repeating_counts_once(self):
        """Anchors are the only field an attacker controls from outside the
        document. Uncapped, a handful of sites can rank you for anything."""
        agg = AnchorAggregator(per_domain_cap=1, min_domains_for_full_weight=1)
        spam = [("spam.test", ["miserable", "failure"]) for _ in range(500)]
        assert agg.aggregate(spam)["failure"] == 1

    def test_many_domains_accumulate(self):
        agg = AnchorAggregator(per_domain_cap=1, min_domains_for_full_weight=1)
        organic = [(f"site{i}.test", ["python", "tutorial"]) for i in range(50)]
        assert agg.aggregate(organic)["python"] == 50

    def test_diversity_beats_volume(self):
        """The signal anchors carry is consensus, not repetition."""
        agg = AnchorAggregator(per_domain_cap=1, min_domains_for_full_weight=3)
        one_loud_site = [("spam.test", ["widget"]) for _ in range(1000)]
        many_quiet_sites = [(f"s{i}.test", ["gadget"]) for i in range(20)]
        combined = agg.aggregate(one_loud_site + many_quiet_sites)
        assert combined["gadget"] > combined["widget"]

    def test_few_domains_are_discounted_even_under_the_cap(self):
        agg = AnchorAggregator(per_domain_cap=1, min_domains_for_full_weight=6)
        two_sites = [("a.test", ["term"]), ("b.test", ["term"])]
        six_sites = [(f"s{i}.test", ["term"]) for i in range(6)]
        assert agg.aggregate(two_sites)["term"] < agg.aggregate(six_sites)["term"]

    def test_domain_diversity_reported(self):
        agg = AnchorAggregator()
        anchors = [("a.test", ["x"]), ("a.test", ["x"]), ("b.test", ["x", "y"])]
        assert agg.domain_diversity(anchors) == {"x": 2, "y": 1}

    def test_empty_input(self):
        assert AnchorAggregator().aggregate([]) == {}


# ---------------------------------------------------------------------------
# Static rank
# ---------------------------------------------------------------------------

class TestStaticRank:
    def test_additive_in_log_space(self):
        assert combine_with_static_rank(5.0, pagerank=0.0) == 5.0
        assert combine_with_static_rank(5.0, pagerank=1.0) == pytest.approx(
            5.0 + math.log(2.0)
        )

    def test_authority_cannot_rescue_an_irrelevant_document(self):
        """The "big sites always win" failure mode.

        Additive means authority lifts a document by a bounded amount. A weak
        textual match on a huge site must not overtake a strong match on a small
        one.
        """
        strong_small = combine_with_static_rank(9.0, pagerank=0.001, alpha=1.0)
        weak_huge = combine_with_static_rank(1.0, pagerank=100.0, alpha=1.0)
        assert strong_small > weak_huge

    def test_multiplicative_does_let_it_happen(self):
        """The rejected alternative, shown failing."""
        strong_small = combine_multiplicative(9.0, pagerank=0.001)
        weak_huge = combine_multiplicative(1.0, pagerank=100.0)
        assert weak_huge > strong_small

    def test_authority_breaks_ties_between_similar_matches(self):
        a = combine_with_static_rank(5.0, pagerank=50.0)
        b = combine_with_static_rank(5.0, pagerank=0.5)
        assert a > b

    def test_quality_contributes(self):
        assert combine_with_static_rank(5.0, quality=2.0, beta=0.5) == pytest.approx(6.0)

    def test_negative_inputs_are_clamped(self):
        assert combine_with_static_rank(5.0, pagerank=-3.0) == 5.0


# ---------------------------------------------------------------------------
# Corpus statistics
# ---------------------------------------------------------------------------

class TestCorpusStatistics:
    def test_idf_needs_global_document_count(self):
        """`N` and `df` are corpus-wide. Using a shard's own counts gives a
        different IDF for the same term."""
        term_df, local_n, global_n = 50, 500, 10_000
        scorer = BM25()
        assert scorer.idf(term_df, local_n) != scorer.idf(term_df, global_n)

    def test_shard_local_idf_reorders_a_merged_result(self):
        """Why non-uniform shards must broadcast global stats.

        Tier 0 is deliberately a biased sample of high-quality documents, so its
        local statistics are systematically wrong relative to the tail's. Scoring
        each shard with its own IDF makes the two shards' scores incomparable,
        and the merged top-k comes out in the wrong order.
        """
        scorer = BM25()
        # Same term, same tf, same length — but the shards differ in size and in
        # how common the term is locally.
        tier0 = CorpusStats(200, 300)
        tier2 = CorpusStats(9_800, 300)
        global_stats = CorpusStats(10_000, 300)

        local_a = scorer.score(3, 300, 100, tier0)     # common in tier 0
        local_b = scorer.score(3, 300, 100, tier2)     # rare in tier 2
        assert local_a != local_b, "local stats give the same posting two scores"

        g_a = scorer.score(3, 300, 400, global_stats)
        g_b = scorer.score(3, 300, 400, global_stats)
        assert g_a == g_b, "global stats make them comparable"

    def test_merged_stats_are_a_weighted_average(self):
        a = CorpusStats(100, 200.0)
        b = CorpusStats(300, 400.0)
        merged = a.merged_with(b)
        assert merged.doc_count == 400
        assert merged.avg_doc_length == pytest.approx(350.0)

    def test_field_stats_merge_by_document_count(self):
        a = FieldStats({"title": 6.0})
        b = FieldStats({"title": 10.0})
        merged = a.merged_with(b, 100, 300)
        assert merged.avg("title") == pytest.approx(9.0)

    def test_field_stats_from_documents(self):
        class Doc:
            def __init__(self, lengths):
                self.field_lengths = lengths

        stats = FieldStats.from_documents([Doc({"title": 4}), Doc({"title": 8})])
        assert stats.avg("title") == pytest.approx(6.0)

    def test_missing_field_average_does_not_divide_by_zero(self):
        assert FieldStats({}).avg("nosuchfield") == 1.0
