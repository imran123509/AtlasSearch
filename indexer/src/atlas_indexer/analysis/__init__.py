"""Text analysis — implements features/TOKENIZATION.md.

Named `analysis` rather than `tokenize` because the latter shadows a stdlib
module, and because tokenisation is only one of the steps here.

The rule everything else serves: **the query is tokenised exactly the same way as
the document**. Any asymmetry produces terms that can never match, and it fails
silently — documents just do not appear.

    from atlas_indexer.analysis import Analyzer

    analyzer = Analyzer()
    doc = analyzer.analyze_document("Block-Max WAND retrieval", lang="en")
    qry = analyzer.analyze_query("block max wand", lang="en")
"""

from __future__ import annotations

from .analyzer import (
    DEFAULT,
    AnalyzedText,
    Analyzer,
    AnalyzerConfig,
    AnalyzerMismatch,
    analyze,
)
from .normalize import FoldPolicy, casefold, fold_diacritics, fold_policy, normalise
from .protect import Protected, find_protected, is_protected
from .segment import Script, character_bigrams, detect_script, segment
from .morphology import light_stem
from .stem import stem_word, stem_words
from .tokens import Token, TokenKind, terms_by_position, token_count

__all__ = [
    "DEFAULT",
    "AnalyzedText",
    "Analyzer",
    "AnalyzerConfig",
    "AnalyzerMismatch",
    "FoldPolicy",
    "Protected",
    "Script",
    "Token",
    "TokenKind",
    "analyze",
    "casefold",
    "character_bigrams",
    "detect_script",
    "find_protected",
    "fold_diacritics",
    "fold_policy",
    "is_protected",
    "light_stem",
    "normalise",
    "segment",
    "stem_word",
    "stem_words",
    "terms_by_position",
    "to_input_document",
    "token_count",
]

# Gap inserted between fields so a phrase query cannot straddle a boundary —
# without it, a title ending "New York" followed by a body starting "Times" would
# match the phrase "york times" that the document never contained.
FIELD_GAP = 100


def to_input_document(parsed, analyzer: Analyzer | None = None, *, static_rank: float = 0.0):
    """Bridge parse output to index input: `ParsedDocument` -> `InputDocument`.

    Title, headings and body are concatenated into one term stream with position
    gaps between them. Real field weighting is BM25F's job and needs per-field
    posting lists; this keeps the fields' *text* without pretending to weight it.
    """
    from ..index.writer import InputDocument

    analyzer = analyzer or DEFAULT
    lang = getattr(parsed.language, "code", None) if parsed.language else None

    terms: dict[str, list[int]] = {}
    position = 0
    total_length = 0

    for text in (parsed.title, " ".join(parsed.headings), parsed.body):
        if not text:
            continue
        analyzed = analyzer.analyze_document(text, lang)
        for term, positions in analyzed.terms.items():
            terms.setdefault(term, []).extend(p + position for p in positions)
        total_length += analyzed.length
        position += analyzed.length + FIELD_GAP

    for positions in terms.values():
        positions.sort()

    return InputDocument(
        external_id=parsed.doc_id,
        terms=terms,
        static_rank=static_rank,
        length=max(total_length, 1),
    )
