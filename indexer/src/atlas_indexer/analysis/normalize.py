"""Unicode normalisation, case folding, and diacritic policy.

Two rules from features/TOKENIZATION.md that are easy to get subtly wrong:

**NFKC, not NFC.** NFKC additionally folds compatibility characters, so the
ligature U+FB01 becomes "fi", circled digits become digits, and fullwidth forms
become ASCII. Without it `ﬁle` and `file` are different terms and a user
searching for one never finds the other.

**Full case folding, not `str.lower()`.** `ß` folds to `ss`; Turkish dotted and
dotless I need locale-aware handling. Python's `str.casefold()` implements
Unicode full case folding; `.lower()` does not.
"""

from __future__ import annotations

import enum
import unicodedata


class FoldPolicy(enum.StrEnum):
    """What to do with diacritics for a given language."""

    FOLD = "fold"                  # accents are decoration: café -> cafe
    KEEP = "keep"                  # they are distinct letters: schön != schon
    TRANSLITERATE = "translit"     # ä -> ae, the language's own convention
    BOTH = "both"                  # uncertain: index both, let scoring decide


# Folding é->e helps an English query reach French text. It DESTROYS meaning in:
#   German      schön != schon, Bär != Bar
#   Swedish/Finnish/Danish/Norwegian   å ä ö are distinct letters, not decorated vowels
#   Turkish     ı and i are different letters
#   Spanish     año != ano — but plain accents (á é í ó ú) are routinely omitted
_POLICY: dict[str, FoldPolicy] = {
    "en": FoldPolicy.FOLD, "fr": FoldPolicy.FOLD, "it": FoldPolicy.FOLD,
    "pt": FoldPolicy.FOLD, "nl": FoldPolicy.FOLD, "ca": FoldPolicy.FOLD,
    "ro": FoldPolicy.FOLD, "pl": FoldPolicy.FOLD, "cs": FoldPolicy.FOLD,
    "vi": FoldPolicy.FOLD,

    "de": FoldPolicy.TRANSLITERATE,

    "sv": FoldPolicy.KEEP, "fi": FoldPolicy.KEEP, "da": FoldPolicy.KEEP,
    "no": FoldPolicy.KEEP, "nb": FoldPolicy.KEEP, "nn": FoldPolicy.KEEP,
    "is": FoldPolicy.KEEP, "tr": FoldPolicy.KEEP, "az": FoldPolicy.KEEP,
    "et": FoldPolicy.KEEP, "hu": FoldPolicy.KEEP,

    "es": FoldPolicy.FOLD,   # with ñ protected below
}

# Characters that survive folding even where the language folds. `ñ` is a letter
# of the Spanish alphabet, not an accented n — año and ano are different words,
# and one of them is embarrassing.
_PROTECTED_BY_LANG: dict[str, frozenset[str]] = {
    "es": frozenset("ñÑ"),
    "pt": frozenset("çÇ"),
    "fr": frozenset("çÇ"),
    "ca": frozenset("çÇ"),
    "ro": frozenset("șşțţȘŞȚŢ"),
}

_GERMAN_MAP = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
                             "Ä": "ae", "Ö": "oe", "Ü": "ue"})

# Turkish/Azeri: the dotted/dotless I pair. `casefold()` turns İ into "i̇"
# (i + combining dot), which is right for Unicode and wrong for Turkish search.
_TURKIC_LOWER = str.maketrans({"I": "ı", "İ": "i"})
_TURKIC = frozenset({"tr", "az"})


def normalise(text: str) -> str:
    """NFKC. Do this before anything else looks at the characters."""
    return unicodedata.normalize("NFKC", text)


def casefold(text: str, lang: str | None = None) -> str:
    """Unicode full case folding, with Turkic locale handling."""
    if lang and lang.lower() in _TURKIC:
        return text.translate(_TURKIC_LOWER).casefold()
    return text.casefold()


def fold_policy(lang: str | None) -> FoldPolicy:
    """Uncertain language means BOTH — index both forms rather than guess.

    Guessing wrong is silent: either the accented word becomes unfindable by an
    unaccented query, or two distinct words collapse into one.
    """
    if not lang:
        return FoldPolicy.BOTH
    return _POLICY.get(lang.lower().split("-")[0], FoldPolicy.BOTH)


def strip_diacritics(text: str, *, protect: frozenset[str] = frozenset()) -> str:
    """Decompose and drop combining marks, leaving `protect` characters intact."""
    out: list[str] = []
    for ch in text:
        if ch in protect:
            out.append(ch)
            continue
        decomposed = unicodedata.normalize("NFD", ch)
        stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
        out.append(unicodedata.normalize("NFC", stripped) or ch)
    return "".join(out)


def fold_diacritics(text: str, lang: str | None) -> list[str]:
    """Return the forms of `text` that should be indexed.

    One entry for a decided policy, two for BOTH. The caller emits them at the
    same position so a phrase query still lines up.
    """
    policy = fold_policy(lang)
    base = lang.lower().split("-")[0] if lang else ""

    if policy is FoldPolicy.KEEP:
        return [text]

    if policy is FoldPolicy.TRANSLITERATE:
        translit = text.translate(_GERMAN_MAP)
        return [text] if translit == text else [text, translit]

    protect = _PROTECTED_BY_LANG.get(base, frozenset())
    folded = strip_diacritics(text, protect=protect)

    if policy is FoldPolicy.BOTH:
        return [text] if folded == text else [text, folded]

    return [folded] if folded == text else [text, folded]


def has_diacritics(text: str) -> bool:
    return any(unicodedata.combining(c) for c in unicodedata.normalize("NFD", text))
