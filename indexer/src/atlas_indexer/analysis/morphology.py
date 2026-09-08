"""Clitic and particle stripping for languages that glue them onto words.

features/TOKENIZATION.md's segmentation table asks for two things that plain
whitespace splitting cannot deliver:

    Korean          morphological analysis; agglutinative, cannot be split on spaces
    Arabic, Hebrew  handle clitics and prefixes; optional vowel stripping

Whitespace splitting leaves `텍스트를` ("text" + object particle) as one token, so
a query for `텍스트` never matches it. Same for Arabic `والكتاب` ("and-the-book")
against a query for `كتاب`.

What is here is **light** stemming — a fixed affix list, in the manner of
Lucene's `ArabicStemmer` and the Larkey et al. light10 stemmer — not a real
morphological analyser. That is a deliberate limit: a proper analyser needs a
lexicon and POS model, and guessing badly is worse than not guessing. The output
is emitted as an *alternative* at the same position as the surface form, exactly
like a stem, so a wrong strip costs precision on one extra term rather than
making the original unfindable.
"""

from __future__ import annotations

# --- Arabic ---------------------------------------------------------------
# Longest first: و+ال must be tried before ال, or "والكتاب" strips to "لكتاب".
#
# Only the definite-article forms plus و, matching the Larkey light10 stemmer.
# The bare single letters ب ك ف ل are prepositions AND extremely common root
# letters — stripping them turns كتاب (kitab, "book") into تاب, and the word
# becomes unfindable by its own spelling. Recall from a clitic strip is not
# worth destroying an exact match.
_AR_PREFIXES: tuple[str, ...] = ("وال", "فال", "بال", "كال", "لل", "ال", "و")
_AR_SUFFIXES: tuple[str, ...] = (
    "ات", "ان", "ون", "ين", "ها", "هم", "هن", "كم", "نا", "ية", "ه", "ة", "ي",
)
_AR_MIN_STEM = 3

# --- Korean ---------------------------------------------------------------
# Josa (particles) attach to nouns. Longest first for the same reason.
_KO_PARTICLES: tuple[str, ...] = (
    "에서부터", "으로부터", "에게서", "한테서", "이라고", "라고",
    "에서", "에게", "한테", "으로", "부터", "까지", "보다", "처럼", "마다",
    "은", "는", "이", "가", "을", "를", "의", "에", "와", "과", "도", "로", "만",
)
# Single-syllable nouns are ordinary in Korean — 책 (book), 물 (water), 산
# (mountain) — so the floor is one character. A floor of two silently refuses to
# strip the particle from exactly the shortest, most common words.
_KO_MIN_STEM = 1


def _strip_arabic(word: str) -> str:
    stem = word
    for prefix in _AR_PREFIXES:
        if stem.startswith(prefix) and len(stem) - len(prefix) >= _AR_MIN_STEM:
            stem = stem[len(prefix):]
            break
    for suffix in _AR_SUFFIXES:
        if stem.endswith(suffix) and len(stem) - len(suffix) >= _AR_MIN_STEM:
            stem = stem[: -len(suffix)]
            break
    return stem


def _strip_korean(word: str) -> str:
    for particle in _KO_PARTICLES:
        if word.endswith(particle) and len(word) - len(particle) >= _KO_MIN_STEM:
            return word[: -len(particle)]
    return word


_STRIPPERS = {"ar": _strip_arabic, "he": _strip_arabic, "ko": _strip_korean}


def supports(lang: str | None) -> bool:
    return bool(lang) and lang.lower().split("-")[0] in _STRIPPERS


def light_stem(word: str, lang: str | None) -> str | None:
    """Return the affix-stripped form, or None when nothing was removed.

    None for "unchanged" matters for the same reason as in `stem.py`: emitting
    an identical token twice at one position doubles the term frequency and makes
    BM25 over-score the document.
    """
    if not word or not supports(lang):
        return None
    stripped = _STRIPPERS[lang.lower().split("-")[0]](word)
    return stripped if stripped and stripped != word else None
