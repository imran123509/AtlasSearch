"""Parameter tuning against a rated set.

features/BM25.md: grid-search `k1` in [0.5, 2.0] and `b` in [0.3, 1.0] against
NDCG — but **expect modest gains**. BM25 is not very sensitive to these, and the
same effort spent on field weights, proximity, or the static-rank prior moves
NDCG considerably more.

`compare_levers` exists to check that claim on your own data rather than take it
on faith, because it is the kind of received wisdom that is true for most corpora
and wrong for some.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field


@dataclass(slots=True)
class RatedQuery:
    """One judged query: terms plus graded relevance per external document id."""

    terms: list[str]
    judgments: dict[str, float] = field(default_factory=dict)
    name: str = ""

    def grade(self, external_id: str) -> float:
        return self.judgments.get(external_id, 0.0)

    @property
    def ideal(self) -> list[float]:
        return sorted(self.judgments.values(), reverse=True)


def dcg(gains: list[float]) -> float:
    """Discounted cumulative gain, log2 discount, rank starting at 1."""
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def ndcg_at_k(ranked_ids: list[str], rated: RatedQuery, k: int = 10) -> float:
    """NDCG@k.

    Note the pool-bias caveat from features/RANKING.md: a document nobody rated
    scores 0 even if it is excellent, so NDCG measured on a shallow pool
    systematically punishes a system that surfaces *new* good documents.
    """
    gains = [rated.grade(doc_id) for doc_id in ranked_ids[:k]]
    ideal = rated.ideal[:k]
    if not ideal or dcg(ideal) == 0:
        return 0.0
    return dcg(gains) / dcg(ideal)


def evaluate(search_fn, queries: list[RatedQuery], *, k: int = 10) -> float:
    """Mean NDCG@k. `search_fn(terms, k)` returns objects with `.external_id`."""
    if not queries:
        return 0.0
    scores = [
        ndcg_at_k([h.external_id for h in search_fn(q.terms, k)], q, k) for q in queries
    ]
    return sum(scores) / len(scores)


@dataclass(slots=True)
class GridResult:
    best: dict[str, float]
    best_score: float
    baseline_score: float
    all_results: list[tuple[dict[str, float], float]] = field(default_factory=list)

    @property
    def improvement(self) -> float:
        return self.best_score - self.baseline_score

    @property
    def relative_improvement(self) -> float:
        return (
            (self.best_score - self.baseline_score) / self.baseline_score
            if self.baseline_score
            else 0.0
        )


def grid_search(
    build_search_fn,
    queries: list[RatedQuery],
    grid: dict[str, list[float]],
    *,
    k: int = 10,
    baseline: dict[str, float] | None = None,
) -> GridResult:
    """Exhaustive grid search.

    `build_search_fn(params) -> search_fn`. Kept as a callback so the same
    harness tunes k1/b, field weights, or the static-rank coefficients without
    knowing anything about them.
    """
    names = list(grid)
    results: list[tuple[dict[str, float], float]] = []

    for combo in itertools.product(*(grid[n] for n in names)):
        params = dict(zip(names, combo))
        results.append((params, evaluate(build_search_fn(params), queries, k=k)))

    results.sort(key=lambda kv: -kv[1])
    best_params, best_score = results[0]

    baseline_score = (
        evaluate(build_search_fn(baseline), queries, k=k)
        if baseline is not None
        else results[-1][1]
    )
    return GridResult(best_params, best_score, baseline_score, results)


DEFAULT_K1_B_GRID: dict[str, list[float]] = {
    "k1": [0.5, 0.9, 1.2, 1.5, 2.0],
    "b": [0.3, 0.5, 0.75, 1.0],
}


def compare_levers(
    queries: list[RatedQuery],
    *,
    tune_k1_b,
    tune_field_weights,
    k: int = 10,
) -> dict[str, float]:
    """Measure which knob actually moves NDCG on this corpus.

    Both arguments are zero-argument callables returning a `GridResult`, so the
    caller decides what "tuning field weights" means for their index.
    """
    k1b = tune_k1_b()
    weights = tune_field_weights()
    return {
        "k1_b_gain": k1b.improvement,
        "field_weight_gain": weights.improvement,
        "field_weights_win_by": weights.improvement - k1b.improvement,
    }
