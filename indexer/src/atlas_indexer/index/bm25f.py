"""BM25F — fielded scoring.

Web documents have structure: a term in the `<title>` means more than the same
term in paragraph 40.

The critical detail is **where the saturation goes**:

    WRONG:  score = sum over fields of  w_f * BM25(field_f)
            saturation applied per field, then summed, so a term stuffed once
            per field bypasses saturation entirely

    RIGHT:  f~(t,d) = sum over fields of  w_f * f(t, field_f) / norm_f

                                  f~(t,d) * (k1 + 1)
            score  = sum over t of IDF(t) * ------------------
                                  f~(t,d) + k1

Weighting per field, then **one** saturation over the combined frequency.

`score_per_field_saturation` below implements the wrong version deliberately, so
that `test_per_field_saturation_enables_stuffing` can demonstrate the attack it
opens rather than just asserting the right answer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .scoring import K1_DEFAULT, BM25, CorpusStats


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One field's weight and its own length-normalisation strength.

    `b` is per-field on purpose: a title is short and its length carries little
    information, so it wants a weaker normalisation than the body.
    """

    name: str
    weight: float
    b: float = 0.75


# Weights from features/BM25.md. `anchors` is the highest AND the most dangerous:
# it is the only field an attacker controls from *outside* the document.
DEFAULT_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("anchors", 10.0, b=0.5),
    FieldSpec("title", 8.0, b=0.3),
    FieldSpec("headings", 3.0, b=0.5),
    FieldSpec("url_text", 2.5, b=0.3),
    FieldSpec("body", 1.0, b=0.75),
    FieldSpec("meta_description", 0.5, b=0.5),
)


@dataclass(slots=True)
class FieldStats:
    """Average length of each field across the corpus.

    Separate from `CorpusStats` because BM25F normalises each field against its
    own average, not against the whole-document average — a 6-word title is not
    a short document.
    """

    avg_lengths: dict[str, float] = field(default_factory=dict)

    def avg(self, name: str) -> float:
        return self.avg_lengths.get(name, 0.0) or 1.0

    @classmethod
    def from_documents(cls, docs) -> FieldStats:
        totals: dict[str, int] = {}
        counts: dict[str, int] = {}
        for doc in docs:
            for name, length in doc.field_lengths.items():
                totals[name] = totals.get(name, 0) + length
                counts[name] = counts.get(name, 0) + 1
        return cls({n: totals[n] / counts[n] for n in totals if counts[n]})

    def merged_with(self, other: FieldStats, self_docs: int, other_docs: int) -> FieldStats:
        total = self_docs + other_docs
        if total == 0:
            return FieldStats({})
        names = set(self.avg_lengths) | set(other.avg_lengths)
        return FieldStats(
            {
                n: (self.avg(n) * self_docs + other.avg(n) * other_docs) / total
                for n in names
            }
        )


@dataclass(frozen=True, slots=True)
class BM25F:
    k1: float = K1_DEFAULT
    fields: tuple[FieldSpec, ...] = DEFAULT_FIELDS

    def spec(self, name: str) -> FieldSpec | None:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    @property
    def weights(self) -> dict[str, float]:
        return {f.name: f.weight for f in self.fields}

    # -- the combination ----------------------------------------------------

    def field_contribution(
        self, name: str, freq: int, field_length: int, stats: FieldStats
    ) -> float:
        """One field's share of the pseudo-frequency, **before** saturation.

        Length-normalised but NOT saturated and NOT weighted — the weight is
        applied by `pseudo_frequency` so it stays a query-time knob, and the
        saturation happens once over the sum.
        """
        if freq <= 0:
            return 0.0
        spec = self.spec(name)
        if spec is None:
            return 0.0
        avg = stats.avg(name)
        norm = 1.0 - spec.b + spec.b * (field_length / avg if avg else 1.0)
        return freq / norm if norm > 0 else float(freq)

    def pseudo_frequency(
        self,
        field_freqs: dict[str, int],
        field_lengths: dict[str, int],
        stats: FieldStats,
    ) -> float:
        """f~(t,d) — the weighted, length-normalised frequency across all fields."""
        total = 0.0
        for spec in self.fields:
            freq = field_freqs.get(spec.name, 0)
            if freq <= 0:
                continue
            total += spec.weight * self.field_contribution(
                spec.name, freq, field_lengths.get(spec.name, 0), stats
            )
        return total

    def saturate(self, pseudo_freq: float) -> float:
        """The single saturation, applied to the combined frequency."""
        if pseudo_freq <= 0:
            return 0.0
        return pseudo_freq * (self.k1 + 1.0) / (pseudo_freq + self.k1)

    # -- scoring ------------------------------------------------------------

    def score_term(
        self,
        field_freqs: dict[str, int],
        field_lengths: dict[str, int],
        doc_freq: int,
        corpus: CorpusStats,
        stats: FieldStats,
    ) -> float:
        idf = BM25(k1=self.k1).idf(doc_freq, corpus.doc_count)
        return idf * self.saturate(
            self.pseudo_frequency(field_freqs, field_lengths, stats)
        )

    def score_per_field_saturation(
        self,
        field_freqs: dict[str, int],
        field_lengths: dict[str, int],
        doc_freq: int,
        corpus: CorpusStats,
        stats: FieldStats,
    ) -> float:
        """The WRONG formulation, kept so its failure can be demonstrated.

        This is what OpenSearch's `most_fields` does, and what most tutorial
        configurations use: saturate each field independently, then sum. Because
        each field saturates on its own, a term stuffed once into every field
        collects near-full credit from each — the exact behaviour k1 exists to
        prevent. See `test_per_field_saturation_enables_stuffing`.
        """
        idf = BM25(k1=self.k1).idf(doc_freq, corpus.doc_count)
        total = 0.0
        for spec in self.fields:
            freq = field_freqs.get(spec.name, 0)
            if freq <= 0:
                continue
            contribution = self.field_contribution(
                spec.name, freq, field_lengths.get(spec.name, 0), stats
            )
            total += spec.weight * self.saturate(contribution)
        return idf * total

    def max_saturation(self) -> float:
        """Supremum of the saturation term: f~ -> infinity gives k1 + 1."""
        return self.k1 + 1.0


# ---------------------------------------------------------------------------
# Static rank
# ---------------------------------------------------------------------------

def combine_with_static_rank(
    text_score: float,
    *,
    pagerank: float = 0.0,
    quality: float = 0.0,
    alpha: float = 1.0,
    beta: float = 1.0,
) -> float:
    """final = BM25F + alpha*log(1+pagerank) + beta*quality.

    **Additive in log space, not multiplicative.** A multiplicative prior lets a
    single high-authority document with a weak textual match beat a perfect match
    on a smaller site — the "big sites always win" failure mode. Additive means
    authority can lift a document a fixed amount but never rescue an irrelevant
    one.
    """
    return text_score + alpha * math.log1p(max(pagerank, 0.0)) + beta * max(quality, 0.0)


def combine_multiplicative(text_score: float, *, pagerank: float = 0.0) -> float:
    """The rejected alternative, kept so the failure can be shown in a test."""
    return text_score * (1.0 + max(pagerank, 0.0))


# ---------------------------------------------------------------------------
# Anchor text capping
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class AnchorAggregator:
    """Build the `anchors` field while bounding what any one site can say.

    Anchor text is the highest-weighted field and the only one an attacker
    controls from outside the document. Without a cap, a handful of coordinated
    sites can make a page rank for anything — the "Google bombing" attack.

    Two limits, because either alone is insufficient:
      * `per_domain_cap` — one domain repeating a phrase 10,000 times counts
        once, however many pages it spans;
      * `min_domains_for_full_weight` — a term backed by one site is discounted
        even under the cap, because the signal anchors carry is *consensus*.
    """

    per_domain_cap: int = 1
    min_domains_for_full_weight: int = 3

    def aggregate(self, anchors: list[tuple[str, list[str]]]) -> dict[str, int]:
        """`[(source_domain, tokens), ...]` -> capped term frequencies."""
        per_domain: dict[str, dict[str, int]] = {}
        for domain, tokens in anchors:
            bucket = per_domain.setdefault(domain, {})
            for token in tokens:
                bucket[token] = bucket.get(token, 0) + 1

        domains_for_term: dict[str, int] = {}
        capped: dict[str, int] = {}
        for bucket in per_domain.values():
            for token, count in bucket.items():
                capped[token] = capped.get(token, 0) + min(count, self.per_domain_cap)
                domains_for_term[token] = domains_for_term.get(token, 0) + 1

        # Discount terms vouched for by too few distinct sites.
        out: dict[str, int] = {}
        for token, count in capped.items():
            domains = domains_for_term[token]
            if domains < self.min_domains_for_full_weight:
                scaled = count * domains / self.min_domains_for_full_weight
                count = max(1, int(scaled))
            out[token] = count
        return out

    def domain_diversity(self, anchors: list[tuple[str, list[str]]]) -> dict[str, int]:
        """Distinct source domains per term — the signal the cap protects."""
        seen: dict[str, set[str]] = {}
        for domain, tokens in anchors:
            for token in set(tokens):
                seen.setdefault(token, set()).add(domain)
        return {token: len(domains) for token, domains in seen.items()}
