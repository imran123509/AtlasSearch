"""Character encoding detection.

Mojibake indexed as real terms is one of the nastier failure modes: the document
indexes fine, the query succeeds, and the terms are garbage that no user will
ever type. There is no error anywhere.

NOTE ON ORDERING. features/HTML-PARSER.md lists the cascade as
"header → meta → BOM → statistical". That puts the BOM third, which is wrong:
the HTML5 encoding sniffing algorithm treats a BOM as **authoritative and
overriding**, ahead of any transport-layer or in-document declaration, because a
BOM is a fact about the bytes while a declaration is a claim about them. A page
served as `charset=iso-8859-1` that begins with a UTF-8 BOM is UTF-8. We
implement BOM → header → meta → statistical.
"""

from __future__ import annotations

import codecs
import re

import structlog

from .models import CharsetResult

log = structlog.get_logger(__name__)

_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)

_META_PRESCAN_BYTES = 1024
_CHARSET_IN_CONTENT = re.compile(rb"""charset\s*=\s*["']?\s*([a-zA-Z0-9_\-:.+]+)""", re.I)
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-:.+]+)""", re.I)

# HTML5 mandates these aliases. The windows-1252 mapping is not pedantry: a large
# share of the web declares iso-8859-1 while actually emitting cp1252 bytes
# (smart quotes, em dashes), and honouring the declaration literally turns every
# curly apostrophe into a replacement character.
_ALIASES = {
    "iso-8859-1": "windows-1252",
    "iso8859-1": "windows-1252",
    "latin1": "windows-1252",
    "latin-1": "windows-1252",
    "l1": "windows-1252",
    "ascii": "windows-1252",
    "us-ascii": "windows-1252",
    "iso-8859-9": "windows-1254",
    "iso-8859-11": "windows-874",
    "tis-620": "windows-874",
    "ks_c_5601-1987": "euc-kr",
    "gb2312": "gbk",
    "gb_2312-80": "gbk",
    "x-sjis": "shift_jis",
    "utf8": "utf-8",
}


def normalise_label(label: str | None) -> str | None:
    if not label:
        return None
    key = label.strip().strip("\"'").lower()
    key = _ALIASES.get(key, key)
    try:
        codecs.lookup(key)
    except LookupError:
        return None
    return key


def _from_bom(raw: bytes) -> tuple[str, int] | None:
    # Longest BOM first so UTF-32-LE is not mistaken for UTF-16-LE.
    for bom, enc in sorted(_BOMS, key=lambda b: -len(b[0])):
        if raw.startswith(bom):
            return enc, len(bom)
    return None


def _from_header(content_type: str | None) -> str | None:
    if not content_type:
        return None
    m = _CHARSET_IN_CONTENT.search(content_type.encode("ascii", "ignore"))
    return normalise_label(m.group(1).decode("ascii", "ignore")) if m else None


def _from_meta(raw: bytes) -> str | None:
    """Prescan the first 1 KB, as the HTML5 algorithm specifies."""
    head = raw[:_META_PRESCAN_BYTES]
    for pattern in (_META_CHARSET, _CHARSET_IN_CONTENT):
        m = pattern.search(head)
        if m:
            enc = normalise_label(m.group(1).decode("ascii", "ignore"))
            if enc:
                return enc
    return None


def _statistical(raw: bytes) -> tuple[str | None, float]:
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(raw[:64_000]).best()
        if best is None or best.encoding is None:
            return None, 0.0
        # charset_normalizer reports chaos (lower is better); invert to confidence.
        return normalise_label(best.encoding), max(0.0, 1.0 - float(best.chaos))
    except Exception as exc:  # noqa: BLE001 - detection must never fail the parse
        log.debug("charset.statistical_failed", error=str(exc))
        return None, 0.0


def detect(raw: bytes, *, content_type: str | None = None) -> CharsetResult:
    """Resolve the encoding of `raw`, in HTML5 precedence order."""
    if bom := _from_bom(raw):
        return CharsetResult(encoding=bom[0], source="bom", confidence=1.0)

    for source, candidate in (("header", _from_header(content_type)), ("meta", _from_meta(raw))):
        if candidate and _decodes_cleanly(raw, candidate):
            return CharsetResult(encoding=candidate, source=source, confidence=0.9)

    enc, conf = _statistical(raw)
    if enc:
        return CharsetResult(encoding=enc, source="statistical", confidence=conf)

    # windows-1252 decodes any byte sequence, so this always terminates.
    return CharsetResult(encoding="windows-1252", source="fallback", confidence=0.1)


def _decodes_cleanly(raw: bytes, encoding: str, *, sample: int = 32_000) -> bool:
    """Validate a *declared* encoding before trusting it.

    A declaration is a claim about the bytes; this checks the claim. Pages
    declaring utf-8 while emitting cp1252 are common enough that skipping this
    check is how mojibake reaches the index.
    """
    try:
        raw[:sample].decode(encoding, errors="strict")
        return True
    except (UnicodeDecodeError, LookupError):
        return False


def decode(raw: bytes, *, content_type: str | None = None) -> tuple[str, CharsetResult]:
    """Decode `raw` to text, stripping any BOM."""
    result = detect(raw, content_type=content_type)
    if bom := _from_bom(raw):
        raw = raw[bom[1]:]
    return raw.decode(result.encoding, errors="replace"), result
