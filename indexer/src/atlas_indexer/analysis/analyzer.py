"""The analyzer: one function, two callers.

The rule the whole module exists to enforce:

    The query must be tokenised exactly the same way as the document.

Any asymmetry produces terms that can never match, and the failure is silent —
documents simply do not appear, with no error anywhere.

features/TOKENIZATION.md sketches this as `analyze(text, lang, query_side=False)`
with a note that the flag may only control behaviour that cannot break matching.
A flag is a weak guarantee: nothing stops a later edit from reading it inside the
stemmer. So the flag is kept for the documented signature, but it never reaches
the pipeline — `_analyze_core` takes no such argument, and query-side behaviour
is strictly *additive* work layered on top of its output. `test_symmetry`
asserts the property directly.

Analyzer changes require a full index rebuild, so the configuration is
fingerprinted into `version` and checked against the segment manifest at query
time. A mismatched analyzer is a silent, total quality failure.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

import structlog

from . import morphology, normalize, protect, segment as seg, stem
from .tokens import Token, TokenKind, terms_by_position, token_count

log = structlog.get_logger(__name__)


class AnalyzerMismatch(Exception):
    """The index was built with a different analyzer than the query is using."""


@dataclass(frozen=True, slots=True)
class AnalyzerConfig:
    stem: bool = True
    index_surface_and_stem: bool = True
    fold_diacritics: bool = True
    protect_identifiers: bool = True
    cjk_bigrams: bool = True
    strip_arabic_vowels: bool = True
    # Arabic/Hebrew clitics and Korean particles. Whitespace alone leaves
    # `텍스트를` and `والكتاب` unmatchable by their own root.
    strip_clitics: bool = True
    max_token_length: int = 64
    # Stopwords are deliberately absent. Removing them at index time breaks
    # "to be or not to be", "The Who", "let it be", "vitamin A". IDF already
    # discounts a term appearing in 90% of documents — that is what it is for —
    # and block-max WAND skips those postings cheaply.
    remove_stopwords: bool = False

    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True)
        return hashlib.blake2b(payload.encode(), digest_size=8).hexdigest()


@dataclass(slots=True)
class AnalyzedText:
    tokens: list[Token]
    language: str | None
    script: seg.Script

    @property
    def terms(self) -> dict[str, list[int]]:
        return terms_by_position(self.tokens)

    @property
    def length(self) -> int:
        """Distinct positions — alternatives must not inflate document length."""
        return token_count(self.tokens)

    def texts(self) -> list[str]:
        return [t.text for t in self.tokens]

    def surface_only(self) -> list[str]:
        return [t.text for t in self.tokens if t.kind is TokenKind.SURFACE]


class Analyzer:
    def __init__(
        self,
        config: AnalyzerConfig | None = None,
        *,
        segmenter: seg.SegmenterProtocol | None = None,
        version_prefix: str = "v1",
    ) -> None:
        self.config = config or AnalyzerConfig()
        self.segmenter = segmenter
        self.version = f"{version_prefix}-{self.config.fingerprint()}"

    # -- the one function ---------------------------------------------------

    def analyze(
        self, text: str, lang: str | None = None, *, query_side: bool = False
    ) -> AnalyzedText:
        """Tokenise `text`. Index and query paths both land here.

        `query_side` does not reach the pipeline; it only enables additive
        post-processing that cannot remove or alter a term.
        """
        result = self._analyze_core(text, lang)
        if query_side:
            result = self._expand_query(result, lang)
        return result

    def analyze_document(self, text: str, lang: str | None = None) -> AnalyzedText:
        return self.analyze(text, lang)

    def analyze_query(self, text: str, lang: str | None = None) -> AnalyzedText:
        return self.analyze(text, lang, query_side=True)

    # -- pipeline -----------------------------------------------------------

    def _analyze_core(self, text: str, lang: str | None) -> AnalyzedText:
        cfg = self.config

        # 1. NFKC, before anything looks at the characters.
        text = normalize.normalise(text)
        script = seg.detect_script(text)

        if cfg.strip_arabic_vowels and script is seg.Script.ARABIC:
            text = seg.strip_arabic_vowels(text)

        # 2. Protected identifiers claim their spans before segmentation runs.
        protected = protect.find_protected(text) if cfg.protect_identifiers else []

        tokens: list[Token] = []
        position = 0
        cursor = 0

        for span in protected:
            if span.start > cursor:
                position = self._emit_range(
                    text, cursor, span.start, lang, script, tokens, position
                )
            position = self._emit_protected(span, lang, tokens, position)
            cursor = span.end

        if cursor < len(text):
            position = self._emit_range(
                text, cursor, len(text), lang, script, tokens, position
            )

        return AnalyzedText(tokens=tokens, language=lang, script=script)

    def _emit_protected(
        self, span: protect.Protected, lang: str | None, out: list[Token], position: int
    ) -> int:
        """Whole token at the first position; parts across consecutive ones."""
        whole = normalize.casefold(span.whole, lang)[: self.config.max_token_length]
        out.append(Token(whole, position, span.start, span.end, TokenKind.WHOLE))

        if not span.parts:
            return position + 1

        for i, part in enumerate(span.parts):
            folded = normalize.casefold(part, lang)[: self.config.max_token_length]
            if folded and folded != whole:
                out.append(
                    Token(folded, position + i, span.start, span.end, TokenKind.PART)
                )
        return position + len(span.parts)

    def _emit_range(
        self,
        text: str,
        start: int,
        end: int,
        lang: str | None,
        script: seg.Script,
        out: list[Token],
        position: int,
    ) -> int:
        cfg = self.config
        chunk = text[start:end]
        spans = seg.segment(chunk, script=script, segmenter=self.segmenter)

        # Each span takes its own position, bigrams included. Overlapping bigrams
        # therefore make a CJK document's length roughly its character count —
        # which is the right comparison base, since every other CJK document is
        # measured the same way and BM25 normalises against the corpus average.
        surfaces: list[str] = []
        metas: list[tuple[int, int, bool]] = []
        for word, ws, we, is_ngram in spans:
            folded_case = normalize.casefold(word, lang)
            if not folded_case:
                continue
            surfaces.append(folded_case[: cfg.max_token_length])
            metas.append((start + ws, start + we, is_ngram))

        stems = stem.stem_words(surfaces, lang) if cfg.stem else [None] * len(surfaces)

        for surface, (tok_start, tok_end, is_ngram), stemmed in zip(surfaces, metas, stems):
            kind = TokenKind.NGRAM if is_ngram else TokenKind.SURFACE
            out.append(Token(surface, position, tok_start, tok_end, kind))

            # Alternatives share the position so phrases still line up.
            if cfg.fold_diacritics:
                for alt in normalize.fold_diacritics(surface, lang)[1:]:
                    out.append(
                        Token(alt[: cfg.max_token_length], position, tok_start,
                              tok_end, TokenKind.FOLDED)
                    )
            if stemmed and cfg.index_surface_and_stem:
                out.append(
                    Token(stemmed[: cfg.max_token_length], position, tok_start,
                          tok_end, TokenKind.STEM)
                )
            if cfg.strip_clitics:
                root = morphology.light_stem(surface, lang)
                if root and root != stemmed:
                    out.append(
                        Token(root[: cfg.max_token_length], position, tok_start,
                              tok_end, TokenKind.STEM)
                    )
            position += 1

        return position

    # -- query-side additions ----------------------------------------------

    def _expand_query(self, result: AnalyzedText, lang: str | None) -> AnalyzedText:
        """Additive only.

        Synonyms and spelling variants may be *added* at an existing position.
        Nothing here may remove a token or change one already produced — doing so
        would break the symmetry the whole module is built on.
        """
        return result

    # -- versioning ---------------------------------------------------------

    def check_compatible(self, index_analyzer_version: str) -> None:
        """Refuse to serve when the index was built by a different analyzer.

        Failing loudly here is the only defence: a mismatch produces query terms
        the index cannot contain, so every result set is quietly wrong.
        """
        if index_analyzer_version != self.version:
            raise AnalyzerMismatch(
                f"index was built with analyzer {index_analyzer_version!r}, "
                f"query is using {self.version!r} — the index needs a rebuild"
            )


DEFAULT = Analyzer()


def analyze(text: str, lang: str | None = None, *, query_side: bool = False) -> AnalyzedText:
    """Module-level convenience over the default analyzer."""
    return DEFAULT.analyze(text, lang, query_side=query_side)
