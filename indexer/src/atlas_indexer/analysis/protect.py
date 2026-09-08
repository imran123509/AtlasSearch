"""Tokens that must not be split.

    C++          -> "c++"       not ["c"]
    .NET         -> ".net"      not ["net"]
    0x80070643   -> intact      <- users search for exactly these
    COVID-19     -> both "covid-19" and ["covid", "19"]
    user@host    -> both whole and parts
    192.168.1.1  -> intact

Losing an exact identifier is one of the most visible quality failures a search
engine can have — somebody pastes an error code and gets nothing — and it is
precisely what dense retrieval is worst at, so the lexical path has to carry it.

The rule is **emit both**: the whole token and its parts. Parts occupy
consecutive positions so a phrase query for "covid 19" still matches, and the
whole sits at the first of those positions so a query for "covid-19" matches too.
"""

from __future__ import annotations

from dataclasses import dataclass

import regex as re


@dataclass(slots=True)
class Protected:
    start: int
    end: int
    whole: str
    parts: list[str]


# Curated technology names. A generic rule cannot distinguish "C++" from a stray
# "a+", so the unambiguous ones are listed. Every real system has this list.
LITERALS: tuple[str, ...] = (
    "c++", "c#", "f#", ".net", "asp.net", "vb.net", "node.js", "next.js",
    "vue.js", "d3.js", "objective-c", "a*", "k8s", "i18n", "l10n", "c--",
)

_LITERAL_RE = re.compile(
    "|".join(re.escape(lit) for lit in sorted(LITERALS, key=len, reverse=True)),
    re.IGNORECASE,
)

# Ordered: the first pattern to match a span wins, so put the specific ones first.
#
# The third field says whether the pieces are worth indexing on their own. For
# COVID-19 they are — somebody will search "covid 19". For 192.168.1.1 they are
# not: "192" and "1" as standalone terms are pure noise, and an IP address is
# only ever searched whole.
_PATTERNS: tuple[tuple[str, str, bool], ...] = (
    # Hex / error codes — the canonical "paste it and search" case.
    ("hex", r"\b0[xX][0-9a-fA-F]+\b", False),
    ("hex_bare", r"\b[0-9a-fA-F]{8,}\b(?=\s|$|[.,;:)])", False),
    # Network and contact identifiers.
    ("ipv4", r"\b\d{1,3}(?:\.\d{1,3}){3}\b", False),
    ("ipv6", r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b", False),
    # The dotted domain is optional: `user@host` (intranet, local accounts) is
    # in the doc's must-not-split list, and requiring a TLD misses it entirely.
    ("email", r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\b", True),
    ("url", r"\bhttps?://\S+", True),
    # Versions and dotted acronyms.
    ("semver", r"\bv?\d+\.\d+(?:\.\d+)*(?:-[\w.]+)?\b", False),
    ("acronym", r"\b(?:\p{L}\.){2,}", False),
    # Identifiers mixing letters and digits: COVID-19, RFC-2616, ISO-8601, SHA-256.
    ("ident", r"\b\p{L}{2,}[-_]\d+\w*\b", True),
    ("ident2", r"\b\p{L}+\d+\p{L}*\b", True),
    # Paths and file names.
    ("path", r"(?:\b\w+)?(?:/[\w.-]+){2,}", True),
    ("filename", r"\b[\w-]+\.(?:tar\.gz|tar\.bz2|[a-z]{1,5})\b", True),
)

_EMIT_PARTS = {name: emit for name, _pattern, emit in _PATTERNS}

_COMBINED = re.compile(
    "|".join(f"(?P<{name}>{pattern})" for name, pattern, _emit in _PATTERNS)
)

_SPLIT = re.compile(r"[^\p{L}\p{N}]+")


def _parts_of(text: str) -> list[str]:
    """Sub-tokens of a protected span, for the "also match the pieces" path."""
    return [p for p in _SPLIT.split(text) if p]


def find_protected(text: str) -> list[Protected]:
    """Locate spans that must survive tokenisation intact.

    Returns non-overlapping spans in document order.
    """
    spans: list[Protected] = []
    claimed: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < e and s < end for s, e in claimed)

    # Literals first: ".NET" would otherwise be eaten by the filename pattern.
    for match in _LITERAL_RE.finditer(text):
        start, end = match.span()
        if overlaps(start, end):
            continue
        whole = match.group(0)
        claimed.append((start, end))
        parts = _parts_of(whole)
        # For "c++" the parts are just ["c"], which is worse than useless as a
        # separate term — it would make every "c++" document match "c". Only
        # keep parts when splitting yields more than one meaningful piece.
        spans.append(Protected(start, end, whole, parts if len(parts) > 1 else []))

    for match in _COMBINED.finditer(text):
        start, end = match.span()
        if overlaps(start, end):
            continue
        whole = match.group(0)
        claimed.append((start, end))
        parts = _parts_of(whole) if _EMIT_PARTS.get(match.lastgroup, True) else []
        spans.append(Protected(start, end, whole, parts if len(parts) > 1 else []))

    spans.sort(key=lambda s: s.start)
    return spans


def is_protected(text: str) -> bool:
    """Whether the whole string is a single protected identifier."""
    found = find_protected(text)
    return len(found) == 1 and found[0].start == 0 and found[0].end == len(text)
