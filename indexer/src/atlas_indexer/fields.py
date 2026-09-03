"""Field extraction.

Fields get separate posting lists and separate weights in BM25F, so what lands in
`title` versus `body` directly changes ranking. See features/BM25.md.

Note what is *absent*: anchor text. It is discovered on *source* pages and must be
shuffled to *target* pages during index build — see features/DISTRIBUTED-INDEXING.md.
A parser cannot know a document's anchors because they live on other documents.
"""

from __future__ import annotations

import json
import re
from urllib.parse import unquote, urlsplit

import structlog
from selectolax.lexbor import LexborHTMLParser

log = structlog.get_logger(__name__)

_ROBOTS_DIRECTIVES = frozenset({
    "noindex", "nofollow", "none", "noarchive", "nosnippet",
    "noimageindex", "notranslate", "noydir", "noodp",
})

_SEP = re.compile(r"[/_\-.+]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_HEX_ISH = re.compile(r"^[0-9a-f]{8,}$", re.I)


def _attr(node, name: str) -> str:
    if node is None:
        return ""
    return (node.attributes or {}).get(name) or ""


def extract_title(tree: LexborHTMLParser) -> str:
    """<title>, then og:title, then the first <h1>.

    og:title before <h1> because it is a deliberate statement of the document's
    name, whereas an <h1> is often site branding.
    """
    for getter in (
        lambda: (tree.css_first("title").text() if tree.css_first("title") else ""),
        lambda: _attr(tree.css_first('meta[property="og:title"]'), "content"),
        lambda: _attr(tree.css_first('meta[name="twitter:title"]'), "content"),
        lambda: (tree.css_first("h1").text() if tree.css_first("h1") else ""),
    ):
        try:
            value = " ".join((getter() or "").split())
        except Exception:  # noqa: BLE001
            continue
        if value:
            return value[:512]
    return ""


def extract_headings(tree: LexborHTMLParser, *, limit: int = 64) -> list[str]:
    out: list[str] = []
    for node in tree.css("h1, h2, h3"):
        text = " ".join(node.text().split())
        if text and len(text) <= 300:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def extract_meta_description(tree: LexborHTMLParser) -> str:
    for selector in (
        'meta[name="description"]',
        'meta[property="og:description"]',
        'meta[name="twitter:description"]',
    ):
        value = " ".join(_attr(tree.css_first(selector), "content").split())
        if value:
            return value[:1024]
    return ""


def extract_canonical(tree: LexborHTMLParser) -> str | None:
    """`rel=canonical` is a hint from an untrusted party.

    Cross-site validation happens in links.py / URL-DE-DUPLICATION.md, not here —
    this only reports what the page claimed.
    """
    href = _attr(tree.css_first('link[rel~="canonical"]'), "href").strip()
    return href or None


def extract_robots_meta(tree: LexborHTMLParser, *, agent: str = "atlassearchbot") -> frozenset[str]:
    """`<meta name="robots">` plus any agent-specific override.

    Honouring the meta tag as well as robots.txt is not optional — `noarchive`
    and `nosnippet` in particular govern what we may *show*, not just fetch.
    """
    found: set[str] = set()
    for node in tree.css("meta[name]"):
        name = _attr(node, "name").strip().lower()
        if name not in ("robots", agent):
            continue
        for token in _attr(node, "content").lower().replace(";", ",").split(","):
            token = token.strip()
            if token in _ROBOTS_DIRECTIVES:
                found.add(token)
    if "none" in found:
        found |= {"noindex", "nofollow"}
    return frozenset(found)


def extract_structured_data(tree: LexborHTMLParser, *, limit: int = 12) -> list[dict]:
    """JSON-LD only. Not scored — used for rich results and as a date source."""
    out: list[dict] = []
    for node in tree.css('script[type="application/ld+json"]'):
        raw = (node.text() or "").strip()
        if not raw or len(raw) > 256_000:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue  # malformed JSON-LD is extremely common; it is not an error
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict):
                out.append(item)
            if len(out) >= limit:
                return out
    return out


def extract_published(tree: LexborHTMLParser, structured: list[dict]) -> str | None:
    for item in structured:
        for key in ("datePublished", "dateCreated", "uploadDate"):
            if isinstance(value := item.get(key), str) and value.strip():
                return value.strip()[:64]

    for selector, attr in (
        ('meta[property="article:published_time"]', "content"),
        ('meta[name="date"]', "content"),
        ('meta[name="publish-date"]', "content"),
        ("time[datetime]", "datetime"),
    ):
        if value := _attr(tree.css_first(selector), attr).strip():
            return value[:64]
    return None


def tokenise_url(url: str) -> str:
    """Turn a URL path into searchable words.

    `/blog/2026/block-max-wand.html` -> `blog 2026 block max wand`. Drops
    opaque identifiers, which are noise in a text field and would otherwise dilute
    the field's length normalisation.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    raw = f"{host} {unquote(parts.path)}"
    raw = _CAMEL.sub(" ", raw)
    words = [w for w in _SEP.sub(" ", raw).split() if w]

    out: list[str] = []
    for w in words:
        if len(w) < 2 or _HEX_ISH.match(w):
            continue
        if w.lower() in ("html", "htm", "php", "aspx", "jsp", "index", "default"):
            continue
        out.append(w.lower())
    return " ".join(out[:64])


def extract_base_href(tree: LexborHTMLParser) -> str | None:
    """<base href> changes how every relative URL on the page resolves.

    Missing this silently produces wrong URLs for the whole document.
    """
    href = _attr(tree.css_first("base[href]"), "href").strip()
    return href or None


def extract_lang_attr(tree: LexborHTMLParser) -> str | None:
    root = tree.css_first("html")
    return (_attr(root, "lang") or _attr(root, "xml:lang")).strip() or None
