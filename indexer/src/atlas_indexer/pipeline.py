"""Orchestration: raw bytes in, ParsedDocument out.

    raw bytes
      → decompress + size cap
      → charset detection (BOM → header → meta → statistical)
      → HTML5 tree build, node/depth capped
      → render decision (static, or hand to the browser pool)
      → boilerplate removal + main content extraction
      → language identification (on extracted text, not markup)
      → field extraction
      → link + anchor extraction
      → ParsedDocument
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import structlog

from . import fields as F
from .boilerplate import TemplateLearner, extract as extract_content
from .charset import decode
from .config import Config
from .language import identify
from .links import extract_links, validate_canonical
from .models import ParsedDocument, RenderDecision
from .parse import ParseRejected, decompress_capped, safe_parse
from .render import decide as decide_render

log = structlog.get_logger(__name__)


def doc_id(url: str) -> str:
    return "d:" + hashlib.blake2b(url.encode(), digest_size=8).hexdigest()


class Parser:
    def __init__(self, config: Config | None = None, *, templates: TemplateLearner | None = None) -> None:
        self.cfg = config or Config()
        self.templates = templates

    def parse(
        self,
        raw: bytes,
        *,
        url: str,
        content_type: str | None = None,
        fetched_at: datetime | None = None,
    ) -> ParsedDocument:
        started = time.perf_counter()
        warnings: list[str] = []
        host = urlsplit(url).hostname or ""

        # Decompress FIRST. Charset detection on still-compressed bytes yields
        # mojibake, and passing pre-decoded text to safe_parse would skip its
        # bomb check entirely — making the defence dead code on the real path.
        raw = decompress_capped(raw, self.cfg.limits)

        text, charset = decode(raw, content_type=content_type)
        if charset.source == "fallback":
            warnings.append("charset_fallback")
        if charset.confidence < 0.5 and charset.source == "statistical":
            warnings.append("charset_low_confidence")

        parsed = safe_parse(raw, self.cfg.limits, text=text)
        if parsed.truncated:
            warnings.append("document_truncated")
        tree = parsed.tree

        # Extract content first: the render decision needs the static text length.
        content = extract_content(
            tree.body, self.cfg.boilerplate, host=host, templates=self.templates
        )
        warnings.extend(content.warnings)

        render, reason = decide_render(
            url=url, raw=raw, static_text=content.text, host=host, cfg=self.cfg.render
        )
        if render.needs_browser:
            log.info("render.required", url=url, decision=str(render), reason=reason)

        if self.templates is not None:
            self.templates.observe(host, content.blocks)

        structured = F.extract_structured_data(tree)
        declared_canonical = F.extract_canonical(tree)
        canonical, canonical_warning = validate_canonical(declared_canonical, url)
        if canonical_warning:
            warnings.append(canonical_warning)

        language = identify(content.text, self.cfg.language, lang_attr=F.extract_lang_attr(tree))
        if language.disagreed:
            # Not an error — but a rising rate of disagreement usually means a
            # templating bug on a large site, and tokenisation branches on this.
            warnings.append("lang_attr_disagreed")

        links, link_warnings = extract_links(
            tree, source_url=url, limits=self.cfg.limits, base_href=F.extract_base_href(tree)
        )
        warnings.extend(link_warnings)

        return ParsedDocument(
            doc_id=doc_id(canonical or url),
            url=url,
            fetched_at=fetched_at or datetime.now(timezone.utc),
            title=F.extract_title(tree),
            headings=F.extract_headings(tree),
            body=content.text,
            meta_description=F.extract_meta_description(tree),
            url_text=F.tokenise_url(url),
            language=language,
            charset=charset,
            render=render,
            canonical_url=canonical,
            robots_meta=F.extract_robots_meta(tree),
            structured_data=structured,
            published=F.extract_published(tree, structured),
            links=links,
            visible_chars=content.visible_chars,
            retained_chars=content.retained_chars,
            blocks_total=len(content.blocks),
            blocks_kept=sum(1 for b in content.blocks if b.is_content),
            parse_ms=(time.perf_counter() - started) * 1000,
            warnings=warnings,
        )

    def try_parse(self, raw: bytes, **kwargs) -> ParsedDocument | None:
        """Parse, returning None instead of raising on a rejected document.

        One bad page must never kill a worker. `ParseRejected` is an expected
        outcome on the open web, not an exception worth propagating.
        """
        try:
            return self.parse(raw, **kwargs)
        except ParseRejected as exc:
            log.info("parse.rejected", url=kwargs.get("url"), reason=exc.reason)
            return None
        except Exception:  # noqa: BLE001
            log.exception("parse.failed", url=kwargs.get("url"))
            return None


def needs_render(doc: ParsedDocument) -> bool:
    return doc.render.needs_browser


def is_audit_render(doc: ParsedDocument) -> bool:
    """Audit renders are compared against their static parse, not indexed twice."""
    return doc.render is RenderDecision.RENDER_AUDIT
