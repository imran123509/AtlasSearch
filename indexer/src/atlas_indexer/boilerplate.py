"""Boilerplate removal and main-content extraction.

70-80% of a typical page's text is navigation, footer and chrome. Indexing it
pollutes every document on a site with the same terms and destroys BM25's
discriminative power — every page on the site starts looking equally relevant
for the site's own vocabulary.

Three layers, cheapest first:

  1. structural   drop <nav>/<footer>/<aside>/<script>… and chrome-ish class names
  2. density      Kohlschütter et al. shallow-text classifier — the workhorse
  3. template     subtract blocks repeated across many pages of the same host
                  (best quality, needs several pages, so it is opt-in)
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from selectolax.lexbor import LexborNode

from .config import BoilerplateConfig
from .models import TextBlock

# Never contribute text.
DROP_TAGS = frozenset({
    "script", "style", "noscript", "template", "svg", "math", "canvas",
    "iframe", "object", "embed", "audio", "video", "map", "area",
    "button", "select", "option", "textarea", "input", "label",
})

# Structurally chrome. Dropping these is crude but it is the cheap first pass.
CHROME_TAGS = frozenset({"nav", "footer", "aside", "form", "dialog", "menu"})

BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "main", "header", "footer", "aside", "nav",
    "h1", "h2", "h3", "h4", "h5", "h6", "li", "dd", "dt", "td", "th", "tr",
    "blockquote", "pre", "figcaption", "figure", "address", "hr", "table",
    "ul", "ol", "dl", "fieldset", "details", "summary",
})

_CHROME_HINT = re.compile(
    r"\b(nav|menu|sidebar|side-bar|footer|header|banner|breadcrumb|masthead|"
    r"cookie|consent|gdpr|newsletter|subscribe|promo|advert|\bads?\b|sponsor|"
    r"share|social|related|recommend|popular|trending|comment|disqus|"
    r"pagination|pager|toolbar|widget|modal|popup|overlay|skip-link)\b",
    re.I,
)
_CONTENT_HINT = re.compile(r"\b(article|content|post|entry|story|main|body-text|prose)\b", re.I)

_WORD = re.compile(r"\w+", re.UNICODE)


def _count_words(text: str) -> int:
    return len(_WORD.findall(text))


@dataclass
class _Accumulator:
    blocks: list[TextBlock] = field(default_factory=list)
    buf: list[str] = field(default_factory=list)
    link_words: int = 0
    tag: str = "p"
    depth: int = 0

    def flush(self, min_words: int) -> None:
        text = " ".join(" ".join(self.buf).split())
        self.buf.clear()
        words = _count_words(text)
        if text and words >= min_words:
            self.blocks.append(
                TextBlock(
                    text=text, tag=self.tag, num_words=words,
                    link_words=min(self.link_words, words), depth=self.depth,
                )
            )
        self.link_words = 0


def _is_chrome(node: LexborNode) -> bool:
    if node.tag in CHROME_TAGS:
        return True
    attrs = node.attributes or {}
    ident = f"{attrs.get('class') or ''} {attrs.get('id') or ''} {attrs.get('role') or ''}"
    if not ident.strip():
        return False
    if _CONTENT_HINT.search(ident) and not _CHROME_HINT.search(ident):
        return False
    return bool(_CHROME_HINT.search(ident))


def build_blocks(body: LexborNode, cfg: BoilerplateConfig, *, drop_chrome: bool = True) -> list[TextBlock]:
    """Flatten the DOM into block-level text runs with their shallow features."""
    acc = _Accumulator()

    def walk(node: LexborNode, depth: int, in_link: bool) -> None:
        if depth > 200:
            return
        tag = node.tag

        if node.is_text_node:
            text = node.text_content or ""
            if text.strip():
                acc.buf.append(text)
                if in_link:
                    acc.link_words += _count_words(text)
            return

        if tag in DROP_TAGS or tag == "-comment":
            return
        if drop_chrome and _is_chrome(node):
            return

        is_block = tag in BLOCK_TAGS
        if is_block:
            acc.flush(cfg.min_block_words)
            acc.tag, acc.depth = tag, depth

        if tag == "br":
            acc.flush(cfg.min_block_words)

        entering_link = in_link or tag == "a"
        for child in node.iter(include_text=True):
            walk(child, depth + 1, entering_link)

        if is_block:
            acc.flush(cfg.min_block_words)
            acc.tag = "p"

    walk(body, 0, False)
    acc.flush(cfg.min_block_words)
    return acc.blocks


def classify(blocks: list[TextBlock], cfg: BoilerplateConfig) -> list[TextBlock]:
    """Kohlschütter et al. (WSDM 2010) shallow-text decision tree.

    Uses only word counts and link density of the current block and its two
    neighbours. No site-specific rules, no ML model to retrain — which is why it
    holds up across a corpus nobody has seen yet.
    """
    n = len(blocks)
    # At the document boundary a neighbour's word count is *undefined*, not zero.
    # Treating it as zero makes every branch that tests `<= 4` or `<= 17` fire,
    # so a short article with navigation above it and nothing below is dropped
    # entirely. The paper's tree assumes interior blocks; use a sentinel that
    # abstains rather than voting for boilerplate.
    UNKNOWN_WORDS = 1 << 20

    for i, cur in enumerate(blocks):
        prev = blocks[i - 1] if i > 0 else None
        nxt = blocks[i + 1] if i + 1 < n else None

        prev_ld = prev.link_density if prev else 0.0
        prev_w = prev.num_words if prev else UNKNOWN_WORDS
        next_w = nxt.num_words if nxt else UNKNOWN_WORDS

        if cur.link_density > cfg.link_density_max:
            cur.is_content, cur.reason = False, "link_density"
        elif prev_ld <= cfg.prev_link_density_max:
            if cur.num_words <= 16:
                if next_w <= 15 and prev_w <= 4:
                    cur.is_content, cur.reason = False, "short_isolated"
                else:
                    cur.is_content, cur.reason = True, "short_in_context"
            else:
                cur.is_content, cur.reason = True, "long"
        else:
            if cur.num_words <= 40 and next_w <= 17:
                cur.is_content, cur.reason = False, "after_links"
            else:
                cur.is_content, cur.reason = True, "long_after_links"

        # Headings inside a kept region are content even when short.
        if cur.tag in ("h1", "h2", "h3", "h4") and cur.link_density <= cfg.link_density_max:
            cur.is_content, cur.reason = True, "heading"

    return blocks


class TemplateLearner:
    """Subtract the DOM text a host repeats across pages.

    The doc calls this the best method and notes it needs several pages from one
    site — which is an argument for grouping parse work by host. Blocks appearing
    on most of a host's sampled pages are chrome by definition, whatever they
    look like structurally.
    """

    def __init__(self, cfg: BoilerplateConfig) -> None:
        self.cfg = cfg
        self._seen: dict[str, Counter[str]] = {}
        self._pages: Counter[str] = Counter()

    def observe(self, host: str, blocks: list[TextBlock]) -> None:
        self._pages[host] += 1
        counter = self._seen.setdefault(host, Counter())
        for text in {b.text for b in blocks}:  # once per page, not per occurrence
            counter[text] += 1

    def is_template(self, host: str, text: str) -> bool:
        pages = self._pages.get(host, 0)
        if pages < self.cfg.template_min_pages:
            return False
        return self._seen[host][text] / pages >= self.cfg.template_repeat_ratio

    def subtract(self, host: str, blocks: list[TextBlock]) -> list[TextBlock]:
        for b in blocks:
            if b.is_content and self.is_template(host, b.text):
                b.is_content, b.reason = False, "site_template"
        return blocks


@dataclass(slots=True)
class Extraction:
    text: str
    blocks: list[TextBlock]
    visible_chars: int
    retained_chars: int
    warnings: list[str] = field(default_factory=list)

    @property
    def retained_ratio(self) -> float:
        return self.retained_chars / self.visible_chars if self.visible_chars else 0.0


def extract(
    body: LexborNode,
    cfg: BoilerplateConfig,
    *,
    host: str | None = None,
    templates: TemplateLearner | None = None,
) -> Extraction:
    """Run all three layers and return the main content plus its diagnostics."""
    # Visible text WITHOUT chrome dropping, so `retained_ratio` measures what the
    # remover actually discarded rather than flattering itself.
    all_blocks = build_blocks(body, cfg, drop_chrome=False)
    visible_chars = sum(len(b.text) for b in all_blocks)

    blocks = classify(build_blocks(body, cfg, drop_chrome=True), cfg)
    if templates is not None and host:
        blocks = templates.subtract(host, blocks)

    kept = [b for b in blocks if b.is_content]
    text = "\n".join(b.text for b in kept)
    retained = len(text)

    warnings: list[str] = []
    # The failure the doc singles out: the remover ate the article and nothing
    # else in the system would notice. Emit a warning so MONITORING can alert
    # on the rate rather than on any single page.
    if visible_chars >= cfg.min_retained_chars and retained < cfg.min_retained_chars:
        if retained / max(visible_chars, 1) < cfg.min_retained_ratio:
            warnings.append("suspiciously_low_retention")
            # Re-running the same classifier would reach the same answer. Fall
            # back to a rule that cannot produce an empty document: keep the
            # substantial, low-link-density blocks regardless of their
            # neighbours. Better a noisy document than a missing one.
            fallback = [
                b for b in all_blocks
                if b.num_words >= 12 and b.link_density <= cfg.link_density_max
            ]
            if sum(len(b.text) for b in fallback) > retained:
                for b in fallback:
                    b.is_content, b.reason = True, "retention_fallback"
                kept = fallback
                warnings.append("used_fallback_extraction")
                text = "\n".join(b.text for b in kept)
                retained = len(text)

    return Extraction(
        text=text,
        blocks=blocks,
        visible_chars=visible_chars,
        retained_chars=retained,
        warnings=warnings,
    )
