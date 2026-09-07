"""Segment construction: documents in, immutable segment out.

The one decision here that everything downstream depends on: **docIDs are
assigned in descending static-rank order**.

That is what makes early termination effective. Posting lists are stored in
docID order for d-gap compression and for intersection, so if docID order also
encodes quality, then walking a list forward means walking from best to worst,
the threshold rises fast, and block-max pruning bites almost immediately.

It is also precisely why re-scoring the corpus is a full rebuild: changing the
static-rank formula changes the docID assignment, which invalidates every
posting list, every d-gap and every block maximum in the index. The structure
that makes retrieval cheap makes the most-frequently-retuned signal the most
expensive to change (features/PAGERANK.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import structlog

from .dictionary import DictionaryWriter, TermInfo
from .postings import PostingsWriter
from .scoring import BM25
from .segment import DocEntry, Manifest, write_segment

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class InputDocument:
    external_id: str
    terms: dict[str, list[int]] = field(default_factory=dict)
    static_rank: float = 0.0
    length: int | None = None

    @property
    def token_count(self) -> int:
        if self.length is not None:
            return self.length
        return sum(len(p) for p in self.terms.values()) or len(self.terms)

    @classmethod
    def from_tokens(
        cls, external_id: str, tokens: list[str], *, static_rank: float = 0.0
    ) -> InputDocument:
        """Build from a token stream, recording positions for phrase queries."""
        terms: dict[str, list[int]] = {}
        for position, token in enumerate(tokens):
            terms.setdefault(token, []).append(position)
        return cls(external_id, terms, static_rank, length=len(tokens))


class IndexBuilder:
    def __init__(self, *, scorer: BM25 | None = None, analyzer_version: str = "v1") -> None:
        self.scorer = scorer or BM25()
        self.analyzer_version = analyzer_version
        self._docs: list[InputDocument] = []

    def add(self, doc: InputDocument) -> None:
        self._docs.append(doc)

    def add_many(self, docs) -> None:
        self._docs.extend(docs)

    def __len__(self) -> int:
        return len(self._docs)

    def build(self, path: Path, *, name: str = "seg") -> Manifest:
        # 1. Static-rank order defines docID order. Ties broken on external id so
        #    a rebuild from identical input produces an identical segment.
        ordered = sorted(self._docs, key=lambda d: (-d.static_rank, d.external_id))

        entries = [
            DocEntry(d.external_id, d.token_count, d.static_rank) for d in ordered
        ]
        total_length = sum(e.length for e in entries)
        avg_length = (total_length / len(entries)) if entries else 0.0

        # 2. Invert. Held in memory here; the Target does this as an external
        #    sort-merge over ~5x10^11 postings/day (DISTRIBUTED-INDEXING.md).
        inverted: dict[str, list[tuple[int, list[int]]]] = {}
        for doc_id, doc in enumerate(ordered):
            for term, positions in doc.terms.items():
                inverted.setdefault(term, []).append((doc_id, positions))

        # 3. Encode, computing each block's saturation ceiling as we go.
        dictionary = DictionaryWriter()
        postings_out = bytearray()
        positions_out = bytearray()

        for term in sorted(inverted):
            writer = PostingsWriter()
            for doc_id, positions in inverted[term]:
                freq = len(positions) or 1
                writer.add(
                    doc_id,
                    freq,
                    saturation=self.scorer.saturation(
                        freq, entries[doc_id].length, avg_length
                    ),
                    positions=positions,
                )
            term_postings, term_positions = writer.finish()

            offset = len(postings_out)
            postings_out.extend(term_postings)
            # Record where this term's positions begin in the shared stream;
            # the skip table stores offsets relative to it.
            positions_offset = len(positions_out)
            positions_out.extend(term_positions)

            dictionary.add(
                term,
                TermInfo(writer.doc_freq, writer.total_tf, offset, positions_offset),
            )

        manifest = write_segment(
            Path(path),
            name=name,
            dictionary=dictionary,
            postings=bytes(postings_out),
            positions=bytes(positions_out),
            docs=entries,
            analyzer_version=self.analyzer_version,
            scorer=self.scorer,
        )
        log.info(
            "index.segment_written",
            name=name, docs=manifest.doc_count, terms=manifest.term_count,
            postings_bytes=len(postings_out), positions_bytes=len(positions_out),
        )
        return manifest
