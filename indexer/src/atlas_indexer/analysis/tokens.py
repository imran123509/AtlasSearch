"""Token types.

Several tokens may share a `position`. That is how "index both forms" works —
the surface form and its stem, or a protected identifier and its parts, occupy
the same slot so phrase queries still line up:

    "COVID-19 cases"
      pos 0: covid-19  (whole, surface)
      pos 0: covid     (part)
      pos 1: 19        (part)      <- note: parts that are separate words advance
      pos 2: cases     (surface)
      pos 2: case      (stem)

A phrase query for "covid 19" matches via the parts; a query for "covid-19"
matches the whole. Losing either is a visible quality failure.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class TokenKind(enum.StrEnum):
    SURFACE = "surface"      # the token as written, after normalisation
    STEM = "stem"            # stemmed form, same position as its surface
    WHOLE = "whole"          # a protected identifier kept intact
    PART = "part"            # a piece of a protected identifier
    FOLDED = "folded"        # diacritics removed, emitted alongside the original
    NGRAM = "ngram"          # CJK character bigram

    @property
    def is_alternative(self) -> bool:
        """Whether this token is an alternative reading of another at the same
        position, rather than its own slot in the text."""
        return self in (TokenKind.STEM, TokenKind.FOLDED, TokenKind.WHOLE)


@dataclass(slots=True)
class Token:
    text: str
    position: int
    start: int = 0
    end: int = 0
    kind: TokenKind = TokenKind.SURFACE

    def __repr__(self) -> str:
        return f"Token({self.text!r}@{self.position}:{self.kind})"


def terms_by_position(tokens: list[Token]) -> dict[str, list[int]]:
    """Collapse a token stream into the `{term: [positions]}` the indexer wants.

    Duplicate (term, position) pairs are collapsed — a surface form that stems
    to itself must not count twice, or its term frequency is inflated and BM25
    over-scores it.
    """
    seen: set[tuple[str, int]] = set()
    out: dict[str, list[int]] = {}
    for token in tokens:
        key = (token.text, token.position)
        if key in seen:
            continue
        seen.add(key)
        out.setdefault(token.text, []).append(token.position)
    return out


def token_count(tokens: list[Token]) -> int:
    """Document length for BM25: distinct positions, not token objects.

    Counting alternatives would make a document look longer purely because we
    chose to index its stems, and length normalisation would then penalise it.
    """
    return len({t.position for t in tokens})
