"""The index: a set of segments, plus the generation that names them.

Reads span every live segment. Crucially they score with **global** corpus stats
rather than each segment's own, because segment-local IDF makes scores
incomparable across segments and the merged top-k comes out in the wrong order.
That is the same hazard features/BM25.md raises for non-uniform shards, and it
appears here at a much smaller scale for the same reason.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import structlog

from .merge import MergeStats, merge_segments, merged_stats, should_merge
from .scoring import BM25, CorpusStats
from .segment import Manifest, Segment, SegmentCorrupt
from .wand import Hit, SearchStats, search, search_exhaustive
from .writer import IndexBuilder, InputDocument

log = structlog.get_logger(__name__)

GENERATION = "generation.json"


@dataclass
class Generation:
    """Names the segments that make up one immutable view of the corpus.

    Publishing a generation is a pointer flip, and so is rolling back to the
    previous one — which is what makes a bad build survivable.
    """

    id: str
    segments: list[str] = field(default_factory=list)
    doc_count: int = 0
    analyzer_version: str = "v1"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    parent: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> Generation:
        return cls(**json.loads(raw))


class Index:
    def __init__(self, path: Path, *, verify: bool = True) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.segments: list[Segment] = []
        self.generation: Generation | None = None
        self._verify = verify
        self.reload()

    # -- lifecycle ----------------------------------------------------------

    def reload(self) -> None:
        gen_file = self.path / GENERATION
        if gen_file.exists():
            self.generation = Generation.from_json(gen_file.read_text(encoding="utf-8"))
            names = self.generation.segments
        else:
            self.generation = None
            names = sorted(p.name for p in self.path.iterdir() if p.is_dir())

        self.segments = []
        for name in names:
            try:
                self.segments.append(Segment(self.path / name, verify=self._verify))
            except SegmentCorrupt as exc:
                # Never serve from a segment that failed verification — a corrupt
                # segment is corrupt in every replica, so this is the only gate.
                log.error("index.segment_rejected", segment=name, error=str(exc))
                raise
            except FileNotFoundError:
                log.warning("index.segment_missing", segment=name)

    def publish(self, generation_id: str | None = None) -> Generation:
        gen = Generation(
            id=generation_id or datetime.now(timezone.utc).strftime("gen-%Y%m%d-%H%M%S"),
            segments=[s.manifest.name for s in self.segments],
            doc_count=self.live_count,
            analyzer_version=(
                self.segments[0].manifest.analyzer_version if self.segments else "v1"
            ),
            parent=self.generation.id if self.generation else None,
        )
        (self.path / GENERATION).write_text(gen.to_json(), encoding="utf-8")
        self.generation = gen
        log.info("index.published", generation=gen.id, segments=len(gen.segments))
        return gen

    # -- writing ------------------------------------------------------------

    def add_documents(
        self,
        docs: list[InputDocument],
        *,
        name: str | None = None,
        scorer: BM25 | None = None,
        analyzer_version: str = "v1",
    ) -> Manifest:
        """Write a new immutable segment. Existing segments are untouched."""
        builder = IndexBuilder(scorer=scorer, analyzer_version=analyzer_version)
        builder.add_many(docs)
        seg_name = name or f"seg_{len(self.segments):04d}"
        manifest = builder.build(self.path / seg_name, name=seg_name)
        self.segments.append(Segment(self.path / seg_name, verify=self._verify))
        return manifest

    def delete(self, external_id: str) -> bool:
        """Tombstone a document wherever it lives. Takes effect immediately."""
        for segment in self.segments:
            if segment.delete_external(external_id):
                log.info("index.deleted", external_id=external_id,
                         segment=segment.manifest.name)
                return True
        return False

    # -- merging ------------------------------------------------------------

    def maybe_merge(self, **kwargs) -> MergeStats | None:
        if not should_merge(self.segments, **kwargs):
            return None
        return self.merge()

    def merge(self, *, name: str | None = None) -> MergeStats:
        if len(self.segments) < 1:
            raise ValueError("nothing to merge")
        target = name or f"seg_merged_{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
        old = [s.path for s in self.segments]

        _manifest, stats = merge_segments(self.segments, self.path / target, name=target)
        self.segments = [Segment(self.path / target, verify=self._verify)]

        # Only remove the inputs once the output verifies.
        for path in old:
            shutil.rmtree(path, ignore_errors=True)
        return stats

    # -- reading ------------------------------------------------------------

    @property
    def doc_count(self) -> int:
        return sum(s.doc_count for s in self.segments)

    @property
    def live_count(self) -> int:
        return sum(s.live_count for s in self.segments)

    @property
    def stats(self) -> CorpusStats:
        return merged_stats(self.segments)

    def search(
        self, terms: list[str], *, k: int = 10, collect: SearchStats | None = None
    ) -> list[Hit]:
        stats = self.stats
        results: list[Hit] = []
        for segment in self.segments:
            hits = search(segment, terms, k=k, stats=stats, collect=collect)
            for hit in hits:
                # docIDs are segment-local; the external id is what identifies a
                # document across the index.
                results.append(Hit(hit.doc_id, hit.score, hit.external_id))
        results.sort(key=lambda h: (-h.score, h.external_id))
        return results[:k]

    def search_exhaustive(self, terms: list[str], *, k: int = 10) -> list[Hit]:
        stats = self.stats
        results: list[Hit] = []
        for segment in self.segments:
            results.extend(search_exhaustive(segment, terms, k=k, stats=stats))
        results.sort(key=lambda h: (-h.score, h.external_id))
        return results[:k]

    def __repr__(self) -> str:
        gen = self.generation.id if self.generation else "unpublished"
        return f"<Index {self.path.name} gen={gen} segments={len(self.segments)} live={self.live_count}>"
