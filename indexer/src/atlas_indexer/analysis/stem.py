"""Stemming — and why the surface form is indexed alongside it.

    | Approach      | Behaviour                                             |
    | Porter/Snowball | Rule-based suffix stripping. Aggressive, and it     |
    |               | collides: `university` and `universities` both become |
    |               | `universiti`, while `universe` becomes `univers`.     |
    | Lemmatisation | Dictionary + POS. `better -> good`. Correct, slower.  |
    | Both          | Index the surface form AND the stem, same position.   |

Indexing both buys recall from the stem and precision from the surface form: an
exact match on the written word still scores higher, because it matches two terms
where a stemmed-only match hits one. Costs roughly 30% more postings.

(The doc gives `university -> univers` as the collision example. Snowball
actually produces `universiti` for that word; the collision is real but between
`university` and `universities`. Verified in `test_snowball_collision_is_real`.)
"""

from __future__ import annotations

import functools

# Snowball's own names for the languages we map ISO codes onto.
_LANG_TO_SNOWBALL: dict[str, str] = {
    "ar": "arabic", "hy": "armenian", "eu": "basque", "ca": "catalan",
    "cs": "czech", "da": "danish", "nl": "dutch", "en": "english",
    "et": "estonian", "fi": "finnish", "fr": "french", "de": "german",
    "el": "greek", "hi": "hindi", "hu": "hungarian", "id": "indonesian",
    "ga": "irish", "it": "italian", "lt": "lithuanian", "ne": "nepali",
    "no": "norwegian", "nb": "norwegian", "nn": "norwegian",
    "pt": "portuguese", "ro": "romanian", "ru": "russian", "sr": "serbian",
    "es": "spanish", "sv": "swedish", "ta": "tamil", "tr": "turkish",
    "yi": "yiddish",
}

# Stemming these is either meaningless (no suffix morphology) or actively harmful.
_NO_STEM = frozenset({"zh", "ja", "ko", "th", "km", "lo", "vi", "he"})


@functools.lru_cache(maxsize=64)
def _stemmer(snowball_name: str):  # noqa: ANN202
    import snowballstemmer

    return snowballstemmer.stemmer(snowball_name)


def supports(lang: str | None) -> bool:
    if not lang:
        return False
    base = lang.lower().split("-")[0]
    return base not in _NO_STEM and base in _LANG_TO_SNOWBALL


def stem_word(word: str, lang: str | None) -> str | None:
    """Return the stem, or None when it is unchanged or stemming does not apply.

    Returning None for an unchanged stem matters: emitting an identical token
    twice at one position would double the term frequency and make BM25
    over-score the document.
    """
    if not supports(lang) or not word:
        return None
    base = lang.lower().split("-")[0]
    try:
        stemmed = _stemmer(_LANG_TO_SNOWBALL[base]).stemWord(word)
    except Exception:  # noqa: BLE001 - never fail an index build over stemming
        return None
    if not stemmed or stemmed == word:
        return None
    return stemmed


def stem_words(words: list[str], lang: str | None) -> list[str | None]:
    """Batch form — Snowball's `stemWords` is markedly faster than a loop."""
    if not supports(lang) or not words:
        return [None] * len(words)
    base = lang.lower().split("-")[0]
    try:
        stemmed = _stemmer(_LANG_TO_SNOWBALL[base]).stemWords(words)
    except Exception:  # noqa: BLE001
        return [None] * len(words)
    return [s if s and s != w else None for w, s in zip(words, stemmed)]
