"""Segment merge — the LSM compaction step.

Merging does four things:

  1. drops tombstoned documents physically (they were filtered at query time)
  2. reassigns docIDs so the merged segment is still in static-rank order
  3. concatenates posting lists in the new docID order
  4. **recomputes every block maximum**

Step 4 is not housekeeping, it is a correctness requirement, and it is the
failure the doc singles out: *"Skip data stale after merge -> wrong results,
silently."*

Why stale maxima are wrong rather than merely suboptimal: block maxima bound the
BM25 saturation term, which contains `|d| / avgdl`. Merging changes `avgdl`, and
if it *rises* the denominator shrinks, saturation rises, and a stored maximum
becomes an **underestimate**. An underestimated bound causes Block-Max WAND to
skip a block that contained a winner. No error is raised. The result is simply
worse, forever, for those queries.

`test_merge_recomputes_block_maxima` and `test_block_max_is_a_true_upper_bound`
are the tests that hold this down.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import structlog

from .dictionary import DictionaryWriter, TermInfo
from .postings import NO_MORE, PostingsWriter
from .scoring import BM25, CorpusStats
from .segment import DocEntry, Manifest, Segment, write_segment

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class MergeStats:
    segments_in: int = 0
    docs_in: int = 0
    docs_dropped: int = 0
    docs_out: int = 0
    terms_out: int = 0


def merge_segments(
    segments: list[Segment],
    out_path: Path,
    *,
    name: str = "merged",
    scorer: BM25 | None = None,
) -> tuple[Manifest, MergeStats]:
    if not segments:
        raise ValueError("nothing to merge")

    scorer = scorer or segments[0].scorer
    analyzer_versions = {s.manifest.analyzer_version for s in segments}
    if len(analyzer_versions) > 1:
        # Merging across analyzer versions would produce a segment whose terms
        # came from two different tokenisers — unmatchable by either.
        raise ValueError(f"cannot merge mixed analyzer versions: {sorted(analyzer_versions)}")

    stats = MergeStats(segments_in=len(segments))

    # --- 1 & 2: live documents, reordered by static rank --------------------
    live: list[tuple[float, str, int, int]] = []  # (-rank, external, seg_idx, old_id)
    for seg_idx, segment in enumerate(segments):
        stats.docs_in += segment.doc_count
        for doc_id in range(segment.doc_count):
            if segment.is_deleted(doc_id):
                stats.docs_dropped += 1
                continue
            entry = segment.docs[doc_id]
            live.append((-entry.static_rank, entry.external_id, seg_idx, doc_id))
    live.sort()

    remap: dict[tuple[int, int], int] = {}
    entries: list[DocEntry] = []
    for new_id, (_neg_rank, _ext, seg_idx, old_id) in enumerate(live):
        remap[(seg_idx, old_id)] = new_id
        entries.append(segments[seg_idx].docs[old_id])
    stats.docs_out = len(entries)

    total_length = sum(e.length for e in entries)
    merged_avgdl = (total_length / len(entries)) if entries else 0.0

    # --- 3 & 4: rebuild postings with recomputed maxima ---------------------
    all_terms: set[str] = set()
    for segment in segments:
        all_terms.update(term for term, _info in segment.terms.terms())

    dictionary = DictionaryWriter()
    postings_out = bytearray()
    positions_out = bytearray()

    for term in sorted(all_terms):
        collected: list[tuple[int, int, list[int]]] = []
        for seg_idx, segment in enumerate(segments):
            cursor = segment.cursor(term)
            if cursor is None:
                continue
            doc = cursor.doc()
            while doc != NO_MORE:
                key = (seg_idx, doc)
                new_id = remap.get(key)
                if new_id is not None:  # None means the doc was tombstoned
                    collected.append((new_id, cursor.freq(), cursor.positions()))
                doc = cursor.next_doc()

        if not collected:
            continue
        collected.sort(key=lambda row: row[0])

        writer = PostingsWriter()
        for new_id, freq, positions in collected:
            writer.add(
                new_id,
                freq,
                # Recomputed against the MERGED avgdl. Copying the old block
                # maxima here is the silent-wrong-results bug.
                saturation=scorer.saturation(freq, entries[new_id].length, merged_avgdl),
                positions=positions,
            )
        term_postings, term_positions = writer.finish()

        offset = len(postings_out)
        postings_out.extend(term_postings)
        positions_offset = len(positions_out)
        positions_out.extend(term_positions)
        dictionary.add(
            term, TermInfo(writer.doc_freq, writer.total_tf, offset, positions_offset)
        )
        stats.terms_out += 1

    manifest = write_segment(
        Path(out_path),
        name=name,
        dictionary=dictionary,
        postings=bytes(postings_out),
        positions=bytes(positions_out),
        docs=entries,
        analyzer_version=analyzer_versions.pop(),
        scorer=scorer,
    )
    log.info(
        "index.merged",
        name=name, segments=stats.segments_in, docs_in=stats.docs_in,
        docs_dropped=stats.docs_dropped, docs_out=stats.docs_out,
    )
    return manifest, stats


def should_merge(segments: list[Segment], *, max_segments: int = 8,
                 delete_ratio: float = 0.25) -> bool:
    """Size-tiered trigger.

    Two reasons to compact: too many segments (every query fans out across all
    of them) or too much tombstoned dead weight (postings decoded then discarded).
    """
    if len(segments) > max_segments:
        return True
    total = sum(s.doc_count for s in segments)
    dead = sum(len(s.deletes) for s in segments)
    return bool(total) and dead / total > delete_ratio


def merged_stats(segments: list[Segment]) -> CorpusStats:
    """Corpus stats across a set of segments, for consistent global scoring."""
    acc = CorpusStats(0, 0.0)
    for segment in segments:
        acc = acc.merged_with(segment.stats)
    return acc
