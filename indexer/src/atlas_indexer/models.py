"""Types produced by the parse stage."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RenderDecision(enum.StrEnum):
    STATIC = "static"
    RENDER_THIN = "render_thin"          # little text + a lot of JS
    RENDER_FRAMEWORK = "render_framework"  # client-side framework fingerprint
    RENDER_ALLOWLIST = "render_allowlist"
    RENDER_AUDIT = "render_audit"        # the 1% sample that keeps us honest

    @property
    def needs_browser(self) -> bool:
        return self is not RenderDecision.STATIC


class LinkRel(enum.StrEnum):
    FOLLOW = "follow"
    NOFOLLOW = "nofollow"
    UGC = "ugc"
    SPONSORED = "sponsored"

    @property
    def passes_authority(self) -> bool:
        """Whether PageRank should flow along this edge.

        All of these are still queued for *discovery* — nofollow means
        "I do not vouch for this", not "do not visit this".
        """
        return self is LinkRel.FOLLOW


@dataclass(slots=True)
class ExtractedLink:
    source_url: str
    target_url: str
    anchor_text: str
    context: str = ""          # ±N chars around the anchor
    rel: LinkRel = LinkRel.FOLLOW
    internal: bool = False
    in_main_content: bool = False  # links in chrome are worth less


@dataclass(slots=True)
class TextBlock:
    """One block-level run of text, with the shallow features used to classify it."""

    text: str
    tag: str
    num_words: int
    link_words: int
    depth: int
    is_content: bool = False
    reason: str = ""

    @property
    def link_density(self) -> float:
        return self.link_words / self.num_words if self.num_words else 0.0


@dataclass(slots=True)
class CharsetResult:
    encoding: str
    source: str          # bom | header | meta | statistical | fallback
    confidence: float


@dataclass(slots=True)
class LanguageResult:
    code: str
    confidence: float
    source: str          # model | attribute | fallback
    attribute: str | None = None
    disagreed: bool = False   # model and `lang` attribute differ — worth watching


@dataclass(slots=True)
class ParsedDocument:
    doc_id: str
    url: str
    fetched_at: datetime

    title: str = ""
    headings: list[str] = field(default_factory=list)
    body: str = ""
    meta_description: str = ""
    url_text: str = ""

    language: LanguageResult | None = None
    charset: CharsetResult | None = None
    render: RenderDecision = RenderDecision.STATIC

    canonical_url: str | None = None
    robots_meta: frozenset[str] = frozenset()
    structured_data: list[dict] = field(default_factory=list)
    published: str | None = None

    links: list[ExtractedLink] = field(default_factory=list)

    # Diagnostics — these are quality signals, not debug output. See MONITORING.md:
    # a drop in `retained_ratio` means the boilerplate remover started eating
    # main content, and nothing else in the system would alert on that.
    visible_chars: int = 0
    retained_chars: int = 0
    blocks_total: int = 0
    blocks_kept: int = 0
    parse_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def retained_ratio(self) -> float:
        return self.retained_chars / self.visible_chars if self.visible_chars else 0.0

    @property
    def is_indexable(self) -> bool:
        return "noindex" not in self.robots_meta and bool(self.body.strip())

    def to_event(self) -> dict:
        """Payload for `pages.parsed`. Text travels here (~8 KB), unlike page bodies."""
        return {
            "doc_id": self.doc_id,
            "url": self.url,
            "canonical_url": self.canonical_url,
            "fetched_at": self.fetched_at.isoformat(),
            "title": self.title,
            "headings": self.headings,
            "body": self.body,
            "meta_description": self.meta_description,
            "url_text": self.url_text,
            "lang": self.language.code if self.language else "und",
            "lang_confidence": self.language.confidence if self.language else 0.0,
            "charset": self.charset.encoding if self.charset else None,
            "render": str(self.render),
            "robots_meta": sorted(self.robots_meta),
            "structured_data": self.structured_data,
            "published": self.published,
            "outlink_count": len(self.links),
            "retained_ratio": round(self.retained_ratio, 4),
            "parse_ms": round(self.parse_ms, 2),
            "warnings": self.warnings,
        }
