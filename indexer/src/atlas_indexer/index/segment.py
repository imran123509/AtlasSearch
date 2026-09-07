"""Immutable index segments.

A segment is a directory of files that is written once and never modified:

    seg_000/
      manifest.json    doc/term counts, corpus stats, analyzer version, checksums
      terms.dict       front-coded term dictionary
      postings.bin     block-max posting lists
      positions.bin    the separate position stream
      docs.bin         forward index: docID -> external id, length, static rank
      deletes.bin      the one mutable file — tombstones, rewritten in place

Immutability is what makes the serving plane read-only: replication is a file
copy, and rollback is remounting the previous generation. It is also why a
corrupt segment is corrupt in *every replica* — replication provides no
protection against it, so the checksums in the manifest are the actual defence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import structlog

from .codec import BLOCK_SIZE, read_f32, read_varint, write_f32, write_varint
from .deletes import DeleteSet
from .dictionary import DictionaryReader, DictionaryWriter, TermInfo
from .postings import PostingCursor
from .scoring import BM25, CorpusStats

log = structlog.get_logger(__name__)

MANIFEST = "manifest.json"
TERMS = "terms.dict"
POSTINGS = "postings.bin"
POSITIONS = "positions.bin"
DOCS = "docs.bin"
DELETES = "deletes.bin"


class SegmentCorrupt(Exception):
    """A checksum or structural invariant failed. Never serve from this segment."""


@dataclass(slots=True)
class DocEntry:
    external_id: str
    length: int
    static_rank: float


@dataclass
class Manifest:
    name: str
    doc_count: int
    term_count: int
    total_length: int
    avg_doc_length: float
    block_size: int = BLOCK_SIZE
    k1: float = 1.2
    b: float = 0.75
    # Checked at query time: a mismatched analyzer is a silent, total quality
    # failure, because the query produces terms the index cannot contain.
    analyzer_version: str = "v1"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    checksums: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> Manifest:
        return cls(**json.loads(raw))

    @property
    def stats(self) -> CorpusStats:
        return CorpusStats(self.doc_count, self.avg_doc_length)


def _checksum(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def write_segment(
    path: Path,
    *,
    name: str,
    dictionary: DictionaryWriter,
    postings: bytes,
    positions: bytes,
    docs: list[DocEntry],
    analyzer_version: str = "v1",
    scorer: BM25 | None = None,
) -> Manifest:
    path.mkdir(parents=True, exist_ok=True)
    scorer = scorer or BM25()

    terms_bytes = dictionary.finish()
    docs_bytes = _encode_docs(docs)

    total_length = sum(d.length for d in docs)
    manifest = Manifest(
        name=name,
        doc_count=len(docs),
        term_count=len(dictionary._terms),  # noqa: SLF001 - same module family
        total_length=total_length,
        avg_doc_length=(total_length / len(docs)) if docs else 0.0,
        k1=scorer.k1,
        b=scorer.b,
        analyzer_version=analyzer_version,
        checksums={
            TERMS: _checksum(terms_bytes),
            POSTINGS: _checksum(postings),
            POSITIONS: _checksum(positions),
            DOCS: _checksum(docs_bytes),
        },
    )

    (path / TERMS).write_bytes(terms_bytes)
    (path / POSTINGS).write_bytes(postings)
    (path / POSITIONS).write_bytes(positions)
    (path / DOCS).write_bytes(docs_bytes)
    (path / DELETES).write_bytes(DeleteSet(len(docs)).to_bytes())
    # Manifest last: its presence is the signal that the segment is complete, so
    # a crash mid-write leaves a directory that is skipped rather than half-read.
    (path / MANIFEST).write_text(manifest.to_json(), encoding="utf-8")
    return manifest


def _encode_docs(docs: list[DocEntry]) -> bytes:
    out = bytearray()
    write_varint(out, len(docs))
    for d in docs:
        raw = d.external_id.encode("utf-8")
        write_varint(out, len(raw))
        out.extend(raw)
        write_varint(out, d.length)
        write_f32(out, d.static_rank)
    return bytes(out)


def _decode_docs(buf: memoryview) -> list[DocEntry]:
    if not len(buf):
        return []
    pos = 0
    count, pos = read_varint(buf, pos)
    out: list[DocEntry] = []
    for _ in range(count):
        n, pos = read_varint(buf, pos)
        external = bytes(buf[pos : pos + n]).decode("utf-8")
        pos += n
        length, pos = read_varint(buf, pos)
        rank, pos = read_f32(buf, pos)
        out.append(DocEntry(external, length, rank))
    return out


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

class Segment:
    def __init__(self, path: Path, *, verify: bool = True) -> None:
        self.path = Path(path)
        manifest_file = self.path / MANIFEST
        if not manifest_file.exists():
            raise SegmentCorrupt(f"{self.path}: no manifest — segment is incomplete")
        self.manifest = Manifest.from_json(manifest_file.read_text(encoding="utf-8"))

        terms_bytes = (self.path / TERMS).read_bytes()
        postings_bytes = (self.path / POSTINGS).read_bytes()
        positions_bytes = (self.path / POSITIONS).read_bytes()
        docs_bytes = (self.path / DOCS).read_bytes()

        if verify:
            self._verify(
                {
                    TERMS: terms_bytes,
                    POSTINGS: postings_bytes,
                    POSITIONS: positions_bytes,
                    DOCS: docs_bytes,
                }
            )

        self.terms = DictionaryReader(terms_bytes)
        self._postings = memoryview(postings_bytes)
        self._positions = memoryview(positions_bytes)
        self.docs = _decode_docs(memoryview(docs_bytes))
        self.scorer = BM25(k1=self.manifest.k1, b=self.manifest.b)

        deletes_file = self.path / DELETES
        self.deletes = (
            DeleteSet.from_bytes(deletes_file.read_bytes())
            if deletes_file.exists()
            else DeleteSet(self.manifest.doc_count)
        )

    def _verify(self, files: dict[str, bytes]) -> None:
        for name, data in files.items():
            expected = self.manifest.checksums.get(name)
            if expected and _checksum(data) != expected:
                raise SegmentCorrupt(f"{self.path}/{name}: checksum mismatch")

    # -- access -------------------------------------------------------------

    @property
    def doc_count(self) -> int:
        return self.manifest.doc_count

    @property
    def live_count(self) -> int:
        return self.deletes.live_count

    @property
    def stats(self) -> CorpusStats:
        return self.manifest.stats

    def term_info(self, term: str) -> TermInfo | None:
        return self.terms.get(term)

    def cursor(self, term: str) -> PostingCursor | None:
        info = self.terms.get(term)
        if info is None:
            return None
        return PostingCursor(
            self._postings, info.postings_offset, self._positions, info.positions_offset
        )

    def doc(self, doc_id: int) -> DocEntry:
        return self.docs[doc_id]

    def doc_length(self, doc_id: int) -> int:
        return self.docs[doc_id].length

    def external_id(self, doc_id: int) -> str:
        return self.docs[doc_id].external_id

    def is_deleted(self, doc_id: int) -> bool:
        return self.deletes.is_deleted(doc_id)

    # -- tombstones ---------------------------------------------------------

    def delete(self, doc_id: int) -> bool:
        """Mark a document deleted and persist immediately.

        Persisted eagerly because this is a compliance surface: a removal that
        is only in memory has not happened.
        """
        if not 0 <= doc_id < self.doc_count:
            raise IndexError(f"docID {doc_id} out of range for {self.manifest.name}")
        changed = self.deletes.delete(doc_id)
        if changed:
            self.flush_deletes()
        return changed

    def delete_external(self, external_id: str) -> bool:
        for doc_id, entry in enumerate(self.docs):
            if entry.external_id == external_id:
                return self.delete(doc_id)
        return False

    def flush_deletes(self) -> None:
        (self.path / DELETES).write_bytes(self.deletes.to_bytes())

    def __repr__(self) -> str:
        return (
            f"<Segment {self.manifest.name} docs={self.doc_count} "
            f"live={self.live_count} terms={self.manifest.term_count}>"
        )
