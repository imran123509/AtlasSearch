"""Block-max inverted index — implements features/INVERTED-INDEX.md.

This is the **Target** artifact. The doc is explicit that the Build should use
Lucene via OpenSearch and not hand-roll an index format, and that guidance still
holds: Lucene's algorithms are the same ones and its implementation is far
better tested. What lives here is the format the Build eventually migrates to,
once per-document JVM overhead and the absence of a tiering primitive stop being
acceptable — plus a reference implementation that makes the mechanics (block
maxima, early termination, merge-time recomputation) directly testable.

    from atlas_indexer.index import Index, InputDocument

    index = Index(path)
    index.add_documents([
        InputDocument.from_tokens("doc-1", ["block", "max", "wand"], static_rank=0.9),
    ])
    index.search(["block", "wand"], k=10)
"""

from .bm25f import (
    DEFAULT_FIELDS,
    BM25F,
    AnchorAggregator,
    FieldSpec,
    FieldStats,
    combine_multiplicative,
    combine_with_static_rank,
)
from .codec import BLOCK_SIZE
from .deletes import DeleteSet
from .dictionary import DictionaryReader, DictionaryWriter, TermInfo
from .fielded import (
    FieldedDocument,
    FieldedIndexBuilder,
    FieldedSegment,
    search_fielded,
    search_fielded_per_field_saturation,
)
from .index import Generation, Index
from .merge import MergeStats, merge_segments, merged_stats, should_merge
from .postings import NO_MORE, PostingCursor, PostingsWriter
from .scoring import BM25, CorpusStats
from .segment import DocEntry, Manifest, Segment, SegmentCorrupt, write_segment
from .tuning import (
    DEFAULT_K1_B_GRID,
    GridResult,
    RatedQuery,
    evaluate,
    grid_search,
    ndcg_at_k,
)
from .wand import Hit, SearchStats, search, search_exhaustive
from .writer import IndexBuilder, InputDocument

__all__ = [
    "BLOCK_SIZE",
    "BM25",
    "BM25F",
    "AnchorAggregator",
    "DEFAULT_FIELDS",
    "DEFAULT_K1_B_GRID",
    "CorpusStats",
    "DeleteSet",
    "DictionaryReader",
    "DictionaryWriter",
    "DocEntry",
    "FieldSpec",
    "FieldStats",
    "FieldedDocument",
    "FieldedIndexBuilder",
    "FieldedSegment",
    "Generation",
    "GridResult",
    "Hit",
    "Index",
    "IndexBuilder",
    "InputDocument",
    "Manifest",
    "RatedQuery",
    "MergeStats",
    "NO_MORE",
    "PostingCursor",
    "PostingsWriter",
    "SearchStats",
    "Segment",
    "SegmentCorrupt",
    "TermInfo",
    "combine_multiplicative",
    "combine_with_static_rank",
    "evaluate",
    "grid_search",
    "merge_segments",
    "merged_stats",
    "ndcg_at_k",
    "search",
    "search_fielded",
    "search_fielded_per_field_saturation",
    "search_exhaustive",
    "should_merge",
    "write_segment",
]
