"""Link and anchor extraction.

Canonicalisation is imported from `atlas_crawler.urlnorm` rather than
reimplemented. That is a deliberate coupling: if the crawler and the indexer
canonicalise differently, the same URL gets two fingerprints depending on which
stage saw it, and de-duplication silently fails. Sharing one implementation is
the only safe option.

It is also the wrong dependency direction — indexer should not depend on crawler.
This belongs in a `common/` package; see the README.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

import structlog
from selectolax.lexbor import LexborHTMLParser, LexborNode

from atlas_crawler.urlnorm import canonicalise, is_crawlable_scheme, registrable_domain

from .boilerplate import BLOCK_TAGS, _is_chrome
from .config import ParseLimits
from .models import ExtractedLink, LinkRel

log = structlog.get_logger(__name__)

_WS = re.compile(r"\s+")

# Schemes that are not documents. Extracting them wastes frontier budget.
_SKIP_SCHEMES = ("javascript:", "mailto:", "tel:", "sms:", "data:", "blob:", "about:", "#")


def _clean(text: str) -> str:
    return _WS.sub(" ", text or "").strip()


def _rel_of(node: LexborNode) -> LinkRel:
    rel = ((node.attributes or {}).get("rel") or "").lower()
    tokens = set(rel.replace(",", " ").split())
    if "sponsored" in tokens:
        return LinkRel.SPONSORED
    if "ugc" in tokens:
        return LinkRel.UGC
    if "nofollow" in tokens:
        return LinkRel.NOFOLLOW
    return LinkRel.FOLLOW


def _nearest_block(node: LexborNode) -> LexborNode | None:
    cur = node.parent
    depth = 0
    while cur is not None and depth < 20:
        if cur.tag in BLOCK_TAGS or cur.tag == "body":
            return cur
        cur = cur.parent
        depth += 1
    return None


def _in_chrome(node: LexborNode) -> bool:
    cur = node.parent
    depth = 0
    while cur is not None and depth < 25:
        if _is_chrome(cur):
            return True
        cur = cur.parent
        depth += 1
    return False


def _context_for(node: LexborNode, anchor: str, window: int) -> str:
    """Text surrounding the anchor, within its block.

    "click here" is useless; the sentence around it is not. Captured here because
    it is the only place the surrounding DOM is still available.
    """
    block = _nearest_block(node)
    if block is None:
        return ""
    try:
        block_text = _clean(block.text(separator=" "))
    except Exception:  # noqa: BLE001
        return ""
    if not block_text:
        return ""
    idx = block_text.find(anchor) if anchor else -1
    if idx < 0:
        return block_text[: window * 2]
    start = max(0, idx - window)
    end = min(len(block_text), idx + len(anchor) + window)
    return block_text[start:end]


def extract_links(
    tree: LexborHTMLParser,
    *,
    source_url: str,
    limits: ParseLimits,
    base_href: str | None = None,
) -> tuple[list[ExtractedLink], list[str]]:
    """Return (links, warnings). Links are canonicalised and de-duplicated."""
    warnings: list[str] = []
    source_domain = registrable_domain(source_url)

    # <base href> changes how every relative URL resolves. Resolve the base itself
    # against the document URL first — a relative <base> is legal and common.
    base = urljoin(source_url, base_href) if base_href else source_url

    seen: set[tuple[str, str]] = set()
    links: list[ExtractedLink] = []
    truncated = False

    for node in tree.css("a[href]"):
        if len(links) >= limits.max_links:
            truncated = True
            break

        href = ((node.attributes or {}).get("href") or "").strip()
        if not href or href.lower().startswith(_SKIP_SCHEMES):
            continue

        try:
            target = canonicalise(urljoin(base, href))
        except Exception:  # noqa: BLE001 - a malformed href is data, not a crash
            continue
        if not is_crawlable_scheme(target) or target == canonicalise(source_url):
            continue

        anchor = _clean(node.text())[: limits.max_anchor_chars]
        key = (target, anchor)
        if key in seen:
            continue
        seen.add(key)

        in_chrome = _in_chrome(node)
        links.append(
            ExtractedLink(
                source_url=source_url,
                target_url=target,
                anchor_text=anchor,
                context=_context_for(node, anchor, limits.anchor_context_chars),
                rel=_rel_of(node),
                internal=registrable_domain(target) == source_domain,
                in_main_content=not in_chrome,
            )
        )

    if truncated:
        # A page with 50,000 links is a link farm or a bug, not a document.
        warnings.append(f"link_cap_reached:{limits.max_links}")
    return links, warnings


def discovery_urls(links: list[ExtractedLink]) -> list[str]:
    """URLs to offer the frontier.

    Includes nofollow targets: `nofollow` means "I do not vouch for this", not
    "do not visit this". Authority flow is a separate question, decided by
    `LinkRel.passes_authority` during the link-graph build.
    """
    return list({link.target_url for link in links})


def authority_edges(links: list[ExtractedLink]) -> list[ExtractedLink]:
    """Edges that PageRank should see."""
    return [link for link in links if link.rel.passes_authority]


def validate_canonical(declared: str | None, source_url: str) -> tuple[str | None, str | None]:
    """Accept `rel=canonical` same-site; treat cross-site as a spam signal.

    A cross-site canonical is any site declaring itself the canonical home of
    your best pages. Honouring it is a hijack primitive.
    """
    if not declared:
        return None, None
    try:
        target = canonicalise(urljoin(source_url, declared))
    except Exception:  # noqa: BLE001
        return None, "canonical_unparseable"
    if registrable_domain(target) != registrable_domain(source_url):
        return None, "canonical_cross_site"
    return target, None
