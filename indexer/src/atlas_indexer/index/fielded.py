"""Fielded indexing and BM25F retrieval.

Layout choice: a term is stored once per field, under a prefixed key
`field\\x00term`. `\\x00` cannot occur in analysed text, so the namespaces cannot
collide. This reuses the whole existing segment machinery — dictionary,
block-max postings, merge, tombstones — without a format change.

What each field's posting stores as its block-max value is the **unweighted,
length-normalised contribution** `tf / norm_f`, not a saturated score. That
matters: the weights stay query-time knobs (features/BM25.md says field weights
move NDCG far more than k1 and b do), and saturation must happen once over the
weighted sum, not per field.

The per-field `b` IS baked in, so changing it needs a rebuild — the same trade
the index already makes for static rank.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field
from pathlib import Path

import structlog

from .bm25f import BM25F, FieldStats
from .dictionary import DictionaryWriter, TermInfo
from .postings import NO_MORE, PostingsWriter
from .scoring import CorpusStats
from .segment import DocEntry, Manifest, Segment, write_segment
from .wand import Hit

log = structlog.get_logger(__name__)

SEP = "\x00"
FIELDS_FILE = "fields.json"


def field_term(field_name: str, term: str) -> str:
    return f"{field_name}{SEP}{term}"


def split_field_term(key: str) -> tuple[str, str]:
    field_name, _, term = key.partition(SEP)
    return field_name, term


@dataclass(slots=True)
class FieldedDocument:
    external_id: str
    # field -> term -> positions
    fields: dict[str, dict[str, list[int]]] = dc_field(default_factory=dict)
    static_rank: float = 0.0
    quality: float = 0.0
    pagerank: float = 0.0

    @property
    def field_lengths(self) -> dict[str, int]:
        return {
            name: sum(len(p) for p in terms.values()) or len(terms)
            for name, terms in self.fields.items()
        }

    @property
    def total_length(self) -> int:
        return sum(self.field_lengths.values()) or 1

    @classmethod
    def from_texts(
        cls,
        external_id: str,
        texts: dict[str, str],
        analyzer,
        *,
        lang: str | None = "en",
        static_rank: float = 0.0,
        pagerank: float = 0.0,
        quality: float = 0.0,
    ) -> FieldedDocument:
        fields = {}
        for name, text in texts.items():
            if text:
                fields[name] = analyzer.analyze_document(text, lang).terms
        return cls(external_id, fields, static_rank, quality, pagerank)


class FieldedIndexBuilder:
    def __init__(self, scorer: BM25F | None = None, *, analyzer_version: str = "v1") -> None:
        self.scorer = scorer or BM25F()
        self.analyzer_version = analyzer_version
        self._docs: list[FieldedDocument] = []

    def add(self, doc: FieldedDocument) -> None:
        self._docs.append(doc)

    def add_many(self, docs) -> None:
        self._docs.extend(docs)

    def build(self, path: Path, *, name: str = "seg") -> Manifest:
        ordered = sorted(self._docs, key=lambda d: (-d.static_rank, d.external_id))
        entries = [DocEntry(d.external_id, d.total_length, d.static_rank) for d in ordered]
        field_lengths = [d.field_lengths for d in ordered]
        stats = FieldStats.from_documents(ordered)

        # Invert per (field, term).
        inverted: dict[str, list[tuple[int, list[int]]]] = {}
        for doc_id, doc in enumerate(ordered):
            for fname, terms in doc.fields.items():
                for term, positions in terms.items():
                    inverted.setdefault(field_term(fname, term), []).append(
                        (doc_id, positions)
                    )

        dictionary = DictionaryWriter()
        postings_out = bytearray()
        positions_out = bytearray()

        for key in sorted(inverted):
            fname, _term = split_field_term(key)
            writer = PostingsWriter()
            for doc_id, positions in inverted[key]:
                freq = len(positions) or 1
                writer.add(
                    doc_id,
                    freq,
                    # Unweighted normalised contribution. The weight and the
                    # saturation are both applied at query time, over the sum.
                    saturation=self.scorer.field_contribution(
                        fname, freq, field_lengths[doc_id].get(fname, 0), stats
                    ),
                    positions=positions,
                )
            term_postings, term_positions = writer.finish()
            offset = len(postings_out)
            postings_out.extend(term_postings)
            positions_offset = len(positions_out)
            positions_out.extend(term_positions)
            dictionary.add(
                key, TermInfo(writer.doc_freq, writer.total_tf, offset, positions_offset)
            )

        manifest = write_segment(
            Path(path),
            name=name,
            dictionary=dictionary,
            postings=bytes(postings_out),
            positions=bytes(positions_out),
            docs=entries,
            analyzer_version=self.analyzer_version,
        )
        # Sidecar rather than a docs.bin format change. The Target packs these
        # into the forward index; at Build scale a JSON file is honest and cheap.
        (Path(path) / FIELDS_FILE).write_text(
            json.dumps(
                {
                    "avg_lengths": stats.avg_lengths,
                    "doc_field_lengths": field_lengths,
                    "k1": self.scorer.k1,
                    "fields": [
                        {"name": f.name, "weight": f.weight, "b": f.b}
                        for f in self.scorer.fields
                    ],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        log.info("index.fielded_segment_written", name=name, docs=len(entries))
        return manifest


class FieldedSegment:
    """A `Segment` plus the per-field length data BM25F needs."""

    def __init__(self, path: Path, *, verify: bool = True) -> None:
        self.segment = Segment(Path(path), verify=verify)
        raw = json.loads((Path(path) / FIELDS_FILE).read_text(encoding="utf-8"))
        self.field_stats = FieldStats(raw["avg_lengths"])
        self.doc_field_lengths: list[dict[str, int]] = raw["doc_field_lengths"]
        self.field_names = [f["name"] for f in raw["fields"]]

    def __getattr__(self, name: str):
        return getattr(self.segment, name)

    def field_lengths(self, doc_id: int) -> dict[str, int]:
        return self.doc_field_lengths[doc_id]

    def cursor(self, field_name: str, term: str):
        return self.segment.cursor(field_term(field_name, term))

    def term_info(self, field_name: str, term: str):
        return self.segment.term_info(field_term(field_name, term))

    def document_frequency(self, term: str) -> int:
        """Distinct documents containing `term` in ANY field.

        Summing per-field document frequencies would over-count a document that
        has the term in both its title and its body, deflating IDF for exactly
        the terms that matter most.
        """
        docs: set[int] = set()
        for name in self.field_names:
            cursor = self.segment.cursor(field_term(name, term))
            if cursor is None:
                continue
            doc = cursor.doc()
            while doc != NO_MORE:
                docs.add(doc)
                doc = cursor.next_doc()
        return len(docs)


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def _gather(fielded: FieldedSegment, term: str) -> dict[int, dict[str, int]]:
    """doc_id -> {field: freq} for one term across every field."""
    out: dict[int, dict[str, int]] = {}
    for name in fielded.field_names:
        cursor = fielded.cursor(name, term)
        if cursor is None:
            continue
        doc = cursor.doc()
        while doc != NO_MORE:
            out.setdefault(doc, {})[name] = cursor.freq()
            doc = cursor.next_doc()
    return out


def search_fielded(
    fielded: FieldedSegment,
    terms: list[str],
    *,
    k: int = 10,
    scorer: BM25F | None = None,
    stats: CorpusStats | None = None,
    alpha: float = 0.0,
    beta: float = 0.0,
    static_ranks: dict[int, tuple[float, float]] | None = None,
) -> list[Hit]:
    """Top-k under BM25F, with the saturation applied once per term.

    `alpha`/`beta` fold in the static-rank prior additively (see
    `bm25f.combine_with_static_rank`); leave them at 0 for pure text scoring.
    """
    scorer = scorer or BM25F()
    stats = stats or fielded.segment.stats

    totals: dict[int, float] = {}
    for term in terms:
        postings = _gather(fielded, term)
        if not postings:
            continue
        doc_freq = len(postings)
        for doc_id, field_freqs in postings.items():
            if fielded.segment.is_deleted(doc_id):
                continue
            totals[doc_id] = totals.get(doc_id, 0.0) + scorer.score_term(
                field_freqs, fielded.field_lengths(doc_id), doc_freq, stats,
                fielded.field_stats,
            )

    if alpha or beta:
        from .bm25f import combine_with_static_rank

        ranks = static_ranks or {}
        for doc_id, score in list(totals.items()):
            pagerank, quality = ranks.get(doc_id, (0.0, 0.0))
            totals[doc_id] = combine_with_static_rank(
                score, pagerank=pagerank, quality=quality, alpha=alpha, beta=beta
            )

    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
    return [
        Hit(doc_id, score, fielded.segment.external_id(doc_id))
        for doc_id, score in ranked
    ]


def search_fielded_per_field_saturation(
    fielded: FieldedSegment,
    terms: list[str],
    *,
    k: int = 10,
    scorer: BM25F | None = None,
    stats: CorpusStats | None = None,
) -> list[Hit]:
    """The WRONG scoring, wired end to end so the stuffing attack is testable."""
    scorer = scorer or BM25F()
    stats = stats or fielded.segment.stats

    totals: dict[int, float] = {}
    for term in terms:
        postings = _gather(fielded, term)
        if not postings:
            continue
        doc_freq = len(postings)
        for doc_id, field_freqs in postings.items():
            if fielded.segment.is_deleted(doc_id):
                continue
            totals[doc_id] = totals.get(
                doc_id, 0.0
            ) + scorer.score_per_field_saturation(
                field_freqs, fielded.field_lengths(doc_id), doc_freq, stats,
                fielded.field_stats,
            )

    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
    return [
        Hit(doc_id, score, fielded.segment.external_id(doc_id))
        for doc_id, score in ranked
    ]
