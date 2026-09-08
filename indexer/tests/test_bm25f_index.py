"""BM25F wired through a real index, plus the tuning harness."""

from __future__ import annotations

import pytest

from atlas_indexer.analysis import Analyzer
from atlas_indexer.index import (
    BM25F,
    DEFAULT_K1_B_GRID,
    FieldSpec,
    FieldedDocument,
    FieldedIndexBuilder,
    FieldedSegment,
    RatedQuery,
    evaluate,
    grid_search,
    ndcg_at_k,
    search_fielded,
    search_fielded_per_field_saturation,
)

ANALYZER = Analyzer()


def build(tmp_path, docs, *, scorer=None, name="seg"):
    builder = FieldedIndexBuilder(scorer)
    builder.add_many(docs)
    builder.build(tmp_path / name, name=name)
    return FieldedSegment(tmp_path / name)


def doc(external_id, *, static_rank=0.5, **texts) -> FieldedDocument:
    return FieldedDocument.from_texts(
        external_id, texts, ANALYZER, static_rank=static_rank
    )


@pytest.fixture
def corpus(tmp_path):
    docs = [
        doc(
            "on-topic-title",
            title="Block max WAND retrieval",
            body="A discussion of posting list traversal and threshold behaviour "
                 "across several paragraphs of ordinary explanatory prose text.",
            static_rank=0.5,
        ),
        doc(
            "on-topic-body",
            title="Some unrelated heading about gardening",
            body="The block max WAND algorithm appears here in the body text only, "
                 "surrounded by a good deal of other unrelated filler wording.",
            static_rank=0.5,
        ),
        doc(
            "off-topic",
            title="Cookery and preserves",
            body="Nothing whatsoever to do with retrieval, indexes or thresholds, "
                 "just a long passage about jam and the making of it at home.",
            static_rank=0.5,
        ),
    ]
    for i in range(40):
        docs.append(
            doc(f"filler-{i}", title=f"Filler {i}", body="ordinary padding text " * 8)
        )
    return build(tmp_path, docs)


class TestFieldedRetrieval:
    def test_title_match_outranks_body_match(self, corpus):
        hits = search_fielded(corpus, ["block", "wand"], k=5)
        ids = [h.external_id for h in hits]
        assert ids.index("on-topic-title") < ids.index("on-topic-body")

    def test_off_topic_document_is_not_returned(self, corpus):
        ids = {h.external_id for h in search_fielded(corpus, ["wand"], k=5)}
        assert "off-topic" not in ids

    def test_absent_term_returns_nothing(self, corpus):
        assert search_fielded(corpus, ["zzznotaterm"], k=5) == []

    def test_document_frequency_counts_documents_not_field_occurrences(self, corpus):
        """Summing per-field DF would over-count a document holding the term in
        both its title and its body, deflating IDF for exactly the terms that
        matter most."""
        df = corpus.document_frequency("block")
        assert df == 2  # two documents, though one has it in two fields

    def test_deleted_document_is_excluded(self, corpus):
        target = search_fielded(corpus, ["wand"], k=5)[0].doc_id
        corpus.segment.delete(target)
        assert target not in {h.doc_id for h in search_fielded(corpus, ["wand"], k=5)}


class TestStuffingEndToEnd:
    @pytest.fixture
    def stuffed_corpus(self, tmp_path):
        stuffed = doc(
            "stuffer",
            title="widget",
            headings="widget",
            body="widget",
            meta_description="widget",
            url_text="widget",
            static_rank=0.5,
        )
        honest = doc(
            "honest",
            title="A careful guide to widgets",
            body="The widget is discussed at length here. A widget has several "
                 "properties, and each widget behaves differently in practice, so "
                 "understanding a widget properly takes some patience and time.",
            static_rank=0.5,
        )
        filler = [
            doc(f"f-{i}", title=f"Other {i}", body="unrelated padding words " * 8)
            for i in range(30)
        ]
        return build(tmp_path, [stuffed, honest, *filler])

    @staticmethod
    def _ratio(hits) -> float:
        by_id = {h.external_id: h.score for h in hits}
        return by_id["stuffer"] / by_id["honest"]

    def test_correct_scoring_bounds_the_stuffers_advantage(self, stuffed_corpus):
        """BM25F **bounds** the stuffing payoff; it does not eliminate it.

        Worth being precise about, because over-claiming here would be
        misleading. A term genuinely present in the title and the URL *is* worth
        more, and BM25F is right to say so. What the single saturation prevents
        is the payoff *scaling* with the number of fields. On this corpus the
        stuffer keeps a 1.07x edge — removing that last few percent is
        anti-spam's job, not the scoring function's.
        """
        assert self._ratio(search_fielded(stuffed_corpus, ["widget"], k=5)) < 1.2

    def test_per_field_saturation_pays_the_stuffer_far_more(self, stuffed_corpus):
        """The bug this design avoids, on a real index: the same document earns
        a decisive advantage instead of a marginal one."""
        wrong = self._ratio(
            search_fielded_per_field_saturation(stuffed_corpus, ["widget"], k=5)
        )
        right = self._ratio(search_fielded(stuffed_corpus, ["widget"], k=5))
        assert wrong > 2.0
        assert wrong > right * 2


class TestStaticRankInRetrieval:
    def test_authority_breaks_a_near_tie(self, corpus):
        base = search_fielded(corpus, ["block", "wand"], k=5)
        ranks = {h.doc_id: (0.0, 0.0) for h in base}
        loser = base[1].doc_id
        ranks[loser] = (500.0, 0.0)

        boosted = search_fielded(
            corpus, ["block", "wand"], k=5, alpha=1.0, static_ranks=ranks
        )
        assert boosted[0].doc_id == loser

    def test_authority_cannot_rescue_an_irrelevant_document(self, corpus):
        base = search_fielded(corpus, ["wand"], k=10)
        ranks = {h.doc_id: (0.0, 0.0) for h in base}
        boosted = search_fielded(corpus, ["wand"], k=10, alpha=1.0, static_ranks=ranks)
        assert "off-topic" not in {h.external_id for h in boosted}


class TestFieldWeightsAreQueryTime:
    def test_reweighting_changes_ranking_without_a_rebuild(self, corpus):
        """Field weights move NDCG far more than k1/b do, so they must be
        tunable against a rated set without re-indexing."""
        title_heavy = BM25F(
            fields=(FieldSpec("title", 20.0, b=0.3), FieldSpec("body", 1.0))
        )
        body_heavy = BM25F(
            fields=(FieldSpec("title", 0.1, b=0.3), FieldSpec("body", 20.0))
        )
        a = [h.external_id for h in search_fielded(corpus, ["block", "wand"], k=3,
                                                   scorer=title_heavy)]
        b = [h.external_id for h in search_fielded(corpus, ["block", "wand"], k=3,
                                                   scorer=body_heavy)]
        assert a[0] == "on-topic-title"
        assert b[0] == "on-topic-body"
        assert a != b


# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------

class TestNDCG:
    def test_perfect_ranking_scores_one(self):
        rated = RatedQuery(["x"], {"a": 3.0, "b": 2.0, "c": 1.0})
        assert ndcg_at_k(["a", "b", "c"], rated, 3) == pytest.approx(1.0)

    def test_reversed_ranking_scores_less(self):
        rated = RatedQuery(["x"], {"a": 3.0, "b": 2.0, "c": 1.0})
        assert ndcg_at_k(["c", "b", "a"], rated, 3) < 1.0

    def test_unjudged_documents_score_zero(self):
        """Pool bias: a document nobody rated scores 0 even if it is excellent,
        so shallow pools punish a system that surfaces new good documents."""
        rated = RatedQuery(["x"], {"a": 3.0})
        assert ndcg_at_k(["unrated"], rated, 1) == 0.0

    def test_no_judgments_scores_zero(self):
        assert ndcg_at_k(["a"], RatedQuery(["x"], {}), 10) == 0.0

    def test_position_matters(self):
        rated = RatedQuery(["x"], {"good": 3.0, "bad": 0.0})
        first = ndcg_at_k(["good", "bad"], rated, 2)
        second = ndcg_at_k(["bad", "good"], rated, 2)
        assert first > second


class TestGridSearch:
    @pytest.fixture
    def rated(self, corpus):
        return corpus, [
            RatedQuery(["block", "wand"], {"on-topic-title": 3.0, "on-topic-body": 2.0}),
            RatedQuery(["wand"], {"on-topic-title": 3.0, "on-topic-body": 3.0}),
        ]

    def test_grid_search_returns_the_best_parameters(self, rated):
        corpus, queries = rated

        def build_search(params):
            scorer = BM25F(
                k1=params["k1"],
                fields=(
                    FieldSpec("title", 8.0, b=params["b"]),
                    FieldSpec("body", 1.0, b=params["b"]),
                ),
            )
            return lambda terms, k: search_fielded(corpus, terms, k=k, scorer=scorer)

        result = grid_search(build_search, queries, DEFAULT_K1_B_GRID, k=5)
        assert set(result.best) == {"k1", "b"}
        assert 0.0 <= result.best_score <= 1.0
        assert len(result.all_results) == 20

    def test_k1_and_b_gains_are_modest(self, rated):
        """The doc's claim, checked rather than assumed: BM25 is not very
        sensitive to k1/b, and the spread across the whole grid is small."""
        corpus, queries = rated

        def build_search(params):
            scorer = BM25F(
                k1=params["k1"],
                fields=(
                    FieldSpec("title", 8.0, b=params["b"]),
                    FieldSpec("body", 1.0, b=params["b"]),
                ),
            )
            return lambda terms, k: search_fielded(corpus, terms, k=k, scorer=scorer)

        result = grid_search(build_search, queries, DEFAULT_K1_B_GRID, k=5)
        scores = [s for _p, s in result.all_results]
        assert max(scores) - min(scores) < 0.35, "k1/b were unexpectedly influential"

    def test_evaluate_averages_across_queries(self, rated):
        corpus, queries = rated
        score = evaluate(lambda terms, k: search_fielded(corpus, terms, k=k), queries, k=5)
        assert 0.0 <= score <= 1.0

    def test_evaluate_with_no_queries(self):
        assert evaluate(lambda terms, k: [], [], k=10) == 0.0
