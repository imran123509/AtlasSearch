"""Tree building, with every axis bounded before a byte is parsed.

Uses lexbor (via selectolax), which is an HTML5 **spec-compliant, error-tolerant**
tree builder. This matters more than speed: real-world HTML is broken in ways
that produce silently wrong text extraction under a permissive parser, and
silently wrong extraction is the worst class of bug in a search engine — quality
degrades with no error signal anywhere.

Never regex. There is no regular expression that parses HTML, and the ones that
nearly work fail on exactly the pages an adversary controls.
"""

from __future__ import annotations

import gzip
import zlib
from dataclasses import dataclass

import structlog
from selectolax.lexbor import LexborHTMLParser

from .config import ParseLimits

log = structlog.get_logger(__name__)


class ParseRejected(Exception):
    """The document was refused before or during parsing. Not a crash."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class ParseOutput:
    tree: LexborHTMLParser
    html: str
    node_count: int
    truncated: bool


def decompress_capped(raw: bytes, limits: ParseLimits) -> bytes:
    """Inflate gzip/deflate while counting output.

    A decompression bomb is a few hundred kilobytes that expands to gigabytes.
    Trusting the declared size, or calling `gzip.decompress` and checking after,
    is how a parse worker gets OOM-killed.
    """
    if not raw[:2] == b"\x1f\x8b":
        return raw

    out = bytearray()
    decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
    chunk_size = 256 * 1024
    for i in range(0, len(raw), chunk_size):
        try:
            out += decompressor.decompress(raw[i : i + chunk_size], limits.max_bytes - len(out) + 1)
        except (zlib.error, gzip.BadGzipFile) as exc:
            raise ParseRejected(f"decompress_failed:{type(exc).__name__}") from exc
        if len(out) > limits.max_bytes:
            raise ParseRejected("decompression_bomb")
    return bytes(out)


def count_nodes(tree: LexborHTMLParser, cap: int) -> tuple[int, bool]:
    """Count nodes, stopping at `cap`.

    A 500,000-node document is not prose. Counting lazily and bailing keeps the
    check itself from becoming the denial of service.
    """
    n = 0
    root = tree.root
    if root is None:
        return 0, False
    for _ in root.traverse(include_text=True):
        n += 1
        if n > cap:
            return n, True
    return n, False


def safe_parse(raw: bytes, limits: ParseLimits, *, text: str | None = None) -> ParseOutput:
    """Parse `raw` (or pre-decoded `text`) into a bounded tree."""
    truncated = False

    if text is None:
        raw = decompress_capped(raw, limits)
        if len(raw) > limits.max_bytes:
            raw = raw[: limits.max_bytes]
            truncated = True
        # Caller normally decodes via charset.decode(); this is the fallback path.
        text = raw.decode("utf-8", errors="replace")
    elif len(text) > limits.max_bytes:
        text = text[: limits.max_bytes]
        truncated = True

    if not text.strip():
        raise ParseRejected("empty_document")

    try:
        tree = LexborHTMLParser(text)
    except Exception as exc:  # noqa: BLE001 - lexbor is tolerant, but never trust that
        raise ParseRejected(f"parse_failed:{type(exc).__name__}") from exc

    # NOTE: lexbor is an HTML5 tree builder, so it synthesises a <body> for
    # essentially any input — feed it an RSS feed and you get a body containing
    # the feed's text. This check therefore only catches a genuinely degenerate
    # tree, NOT "this was not HTML". Non-HTML is filtered by content-type before
    # the parser, and by `ParsedDocument.is_indexable` after it.
    if tree.body is None:
        raise ParseRejected("no_body")

    node_count, over = count_nodes(tree, limits.max_nodes)
    if over:
        raise ParseRejected(f"node_cap_exceeded:{limits.max_nodes}")

    return ParseOutput(tree=tree, html=text, node_count=node_count, truncated=truncated)


def parse_in_subprocess(raw: bytes, limits: ParseLimits) -> ParseOutput:  # pragma: no cover
    """Placeholder for the hard-isolation path.

    features/HTML-PARSER.md calls for a hard timeout and memory cap **in a
    subprocess**, because a parser that hangs or balloons on hostile input
    otherwise takes a worker thread with it. The in-process caps above handle
    size and node count; they cannot bound wall time or RSS.

    The pipeline runs parses through a ProcessPoolExecutor when
    `Config.limits.timeout_seconds > 0` — see pipeline.parse_document. This
    function exists as the documented seam for a stricter implementation
    (rlimit/job-object caps per worker), which is not wired up yet.
    """
    raise NotImplementedError("use pipeline.parse_document, which pools processes")
