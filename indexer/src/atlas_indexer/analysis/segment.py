"""Script detection and segmentation.

    | Script family        | Approach                                          |
    | Latin/Cyrillic/Greek | Unicode word break (UAX #29) + punctuation rules   |
    | Chinese, Japanese    | dictionary + statistical, PLUS character bigrams   |
    | Korean               | morphological; agglutinative, no space splitting   |
    | Thai, Khmer, Lao     | dictionary-based; no spaces between words          |
    | Arabic, Hebrew       | clitics and prefixes; optional vowel stripping     |

For CJK we index **both** segmented words and character bigrams. Segmentation
errors are common, and bigrams give a recall floor when the segmenter is wrong —
which matters more than the extra postings cost, because a segmentation failure
is otherwise silent.

The Build ships bigrams only. A real segmenter needs a language model, and
guessing word boundaries badly is worse than not guessing: `SegmenterProtocol`
is the seam where one plugs in.
"""

from __future__ import annotations

import enum
import unicodedata
from typing import Protocol

import regex as re


class Script(enum.StrEnum):
    LATIN = "latin"          # also Cyrillic, Greek — same segmentation rules
    CJK = "cjk"              # Han, Hiragana, Katakana
    KOREAN = "korean"
    THAI = "thai"            # also Khmer, Lao — no inter-word spaces
    ARABIC = "arabic"        # also Hebrew
    OTHER = "other"

    @property
    def has_word_spaces(self) -> bool:
        """Whether whitespace reliably marks word boundaries in this script."""
        return self in (Script.LATIN, Script.ARABIC, Script.KOREAN, Script.OTHER)


_SCRIPT_RANGES: tuple[tuple[int, int, Script], ...] = (
    (0x0590, 0x06FF, Script.ARABIC),   # Hebrew + Arabic
    (0x0E00, 0x0E7F, Script.THAI),
    (0x0E80, 0x0EFF, Script.THAI),     # Lao
    (0x1780, 0x17FF, Script.THAI),     # Khmer
    (0x1100, 0x11FF, Script.KOREAN),
    (0x3130, 0x318F, Script.KOREAN),
    (0xAC00, 0xD7AF, Script.KOREAN),
    (0x3040, 0x30FF, Script.CJK),      # Hiragana + Katakana
    (0x3400, 0x4DBF, Script.CJK),
    (0x4E00, 0x9FFF, Script.CJK),
    (0xF900, 0xFAFF, Script.CJK),
)

_WORD = re.compile(r"[\p{L}\p{N}\p{M}]+(?:['’][\p{L}]+)*")
_CJK_CHAR = re.compile(r"[\p{Han}\p{Hiragana}\p{Katakana}]")


def script_of_char(ch: str) -> Script:
    cp = ord(ch)
    for lo, hi, script in _SCRIPT_RANGES:
        if lo <= cp <= hi:
            return script
    if ch.isalpha():
        return Script.LATIN
    return Script.OTHER


def detect_script(text: str, *, sample: int = 512) -> Script:
    """Dominant script of `text`, by letter count."""
    counts: dict[Script, int] = {}
    for ch in text[:sample]:
        if not ch.isalnum():
            continue
        script = script_of_char(ch)
        counts[script] = counts.get(script, 0) + 1
    if not counts:
        return Script.OTHER
    # OTHER only wins if nothing else appeared at all.
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0] == Script.OTHER))
    return ranked[0][0]


class SegmenterProtocol(Protocol):
    """Seam for a real word segmenter (dictionary + statistical model)."""

    def segment(self, text: str) -> list[tuple[str, int, int]]:
        """Return (word, start, end) triples."""
        ...


def segment_spaced(text: str) -> list[tuple[str, int, int]]:
    """Whitespace-and-punctuation segmentation for scripts that have spaces.

    Apostrophes inside words are kept (`don't`, `l'homme`) because splitting them
    produces the stopword-ish fragments that make phrase queries fail.
    """
    return [(m.group(0), m.start(), m.end()) for m in _WORD.finditer(text)]


def character_bigrams(text: str, start_offset: int = 0) -> list[tuple[str, int, int]]:
    """Overlapping character bigrams — the CJK recall floor.

    A single character is often a word on its own, so a lone character is also
    emitted; otherwise a one-character query could never match.
    """
    chars = [(ch, start_offset + i) for i, ch in enumerate(text) if not ch.isspace()]
    if not chars:
        return []
    if len(chars) == 1:
        ch, pos = chars[0]
        return [(ch, pos, pos + 1)]
    return [
        (chars[i][0] + chars[i + 1][0], chars[i][1], chars[i + 1][1] + 1)
        for i in range(len(chars) - 1)
    ]


def segment_cjk(
    text: str, *, segmenter: SegmenterProtocol | None = None
) -> list[tuple[str, int, int]]:
    """Words if a segmenter is available, bigrams either way.

    Both are emitted: the segmenter is the precision path and the bigrams are the
    recall floor for when it is wrong.
    """
    out: list[tuple[str, int, int]] = []
    if segmenter is not None:
        out.extend(segmenter.segment(text))
    out.extend(character_bigrams(text))
    return out


def script_runs(text: str) -> list[tuple[str, int, Script]]:
    """Split `text` into maximal script-homogeneous runs.

    Needed because the *document's* dominant script says nothing about any
    individual run. "日本語のテキスト and English" is majority-Latin by character
    count, so dispatching on the document script would leave the Japanese
    unsegmented — one giant token nobody can ever match.
    """
    runs: list[tuple[str, int, Script]] = []
    start = 0
    current: Script | None = None

    for i, ch in enumerate(text):
        script = script_of_char(ch) if ch.isalnum() else current
        if script is None:
            script = Script.OTHER
        if current is None:
            current, start = script, i
        elif script is not current:
            runs.append((text[start:i], start, current))
            current, start = script, i
    if current is not None and start < len(text):
        runs.append((text[start:], start, current))
    return runs


def segment(
    text: str,
    *,
    script: Script | None = None,
    segmenter: SegmenterProtocol | None = None,
) -> list[tuple[str, int, int, bool]]:
    """Split `text` into (word, start, end, is_ngram) spans.

    Dispatch is per run, not per document, so a Japanese page quoting an English
    product name keeps both. `script` is accepted as a hint but never overrides
    what a run actually contains.
    """
    out: list[tuple[str, int, int, bool]] = []

    for run, offset, run_script in script_runs(text):
        if not run.strip():
            continue

        if run_script in (Script.CJK, Script.THAI):
            if segmenter is not None:
                out.extend(
                    (w, offset + s, offset + e, False) for w, s, e in segmenter.segment(run)
                )
            out.extend((w, s, e, True) for w, s, e in character_bigrams(run, offset))
        else:
            out.extend(
                (w, offset + s, offset + e, False) for w, s, e in segment_spaced(run)
            )

    out.sort(key=lambda t: (t[1], t[2]))
    return out


def strip_arabic_vowels(text: str) -> str:
    """Remove Arabic/Hebrew vowel marks, which are usually omitted when typing."""
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c)
    )
