from __future__ import annotations

import codecs
import gzip

import pytest

from atlas_indexer.boilerplate import TemplateLearner
from atlas_indexer.config import Config, RenderConfig
from atlas_indexer.models import RenderDecision
from atlas_indexer.parse import ParseRejected
from atlas_indexer.pipeline import Parser, doc_id

URL = "https://example.com/papers/bmw.html"


class TestEndToEnd:
    def test_all_fields_populated(self, parser, article):
        doc = parser.parse(article, url=URL)
        assert doc.title == "Block-Max Indexes Explained"
        assert doc.headings[0] == "Block-Max Indexes Explained"
        assert "How block upper bounds" in doc.meta_description
        assert doc.canonical_url == "https://example.com/papers/bmw"
        assert doc.published == "2026-03-14"
        assert doc.language.code == "en"
        assert doc.charset.encoding == "utf-8"
        assert doc.url_text == "example com papers bmw"
        assert doc.structured_data[0]["@type"] == "Article"

    def test_chrome_excluded_article_kept(self, parser, article):
        doc = parser.parse(article, url=URL)
        assert "running threshold" in doc.body
        assert "Privacy" not in doc.body
        assert "Alpha" not in doc.body

    def test_links_extracted_and_classified(self, parser, article):
        doc = parser.parse(article, url=URL)
        targets = {link.target_url: link for link in doc.links}
        paper = targets["https://other.test/paper.pdf"]
        assert str(paper.rel) == "nofollow"
        assert paper.internal is False
        assert paper.in_main_content is True

    def test_doc_id_follows_the_canonical_url(self, parser, article):
        """Two URLs that declare the same canonical must produce one doc_id."""
        doc = parser.parse(article, url=URL)
        assert doc.doc_id == doc_id("https://example.com/papers/bmw")

    def test_diagnostics_present(self, parser, article):
        doc = parser.parse(article, url=URL)
        assert doc.visible_chars > 0
        assert 0 < doc.retained_ratio <= 1.0
        assert doc.blocks_kept > 0
        assert doc.parse_ms >= 0
        assert doc.warnings == []

    def test_event_carries_text_but_stays_small(self, parser, article):
        event = parser.parse(article, url=URL).to_event()
        assert event["body"]
        assert set(event) >= {"doc_id", "url", "title", "body", "lang", "outlink_count"}


class TestRobotsMetaHandling:
    def test_noindex_makes_the_document_unindexable(self, parser):
        html = b'<html><head><meta name="robots" content="noindex"></head><body><p>Some real body text here that is long enough to keep.</p></body></html>'
        doc = parser.parse(html, url=URL)
        assert "noindex" in doc.robots_meta
        assert doc.is_indexable is False

    def test_an_empty_body_is_not_indexable(self, parser):
        doc = parser.parse(b"<html><body><nav><a href='/'>H</a></nav></body></html>", url=URL)
        assert doc.is_indexable is False


class TestCharsetIntegration:
    def test_cp1252_page_declared_as_latin1(self, parser):
        raw = ('<html><head><meta charset="iso-8859-1"></head><body><p>'
               'The caf\x92s report covers the quarterly figures and the board review.'
               "</p></body></html>").encode("latin-1")
        doc = parser.parse(raw, url=URL)
        assert doc.charset.encoding == "windows-1252"
        assert "�" not in doc.body

    def test_bom_overrides_the_declared_charset(self, parser):
        raw = codecs.BOM_UTF8 + '<html><head><meta charset="shift_jis"></head><body><p>The quarterly report covers everything relevant.</p></body></html>'.encode()
        doc = parser.parse(raw, url=URL)
        assert doc.charset.encoding == "utf-8"
        assert doc.charset.source == "bom"


class TestRenderIntegration:
    def test_spa_shell_is_flagged_for_rendering(self):
        cfg = Config(render=RenderConfig(audit_sample_rate=0.0))
        raw = (b'<html><body><div id="root"></div><script>'
               + b"x" * 40_000 + b"</script></body></html>")
        doc = Parser(cfg).parse(raw, url=URL)
        assert doc.render.needs_browser
        assert doc.render in (RenderDecision.RENDER_FRAMEWORK, RenderDecision.RENDER_THIN)

    def test_normal_article_is_static(self, article):
        cfg = Config(render=RenderConfig(audit_sample_rate=0.0))
        assert Parser(cfg).parse(article, url=URL).render is RenderDecision.STATIC


class TestWarnings:
    def test_cross_site_canonical_warns_and_is_dropped(self, parser):
        html = b'<html><head><link rel="canonical" href="https://attacker.test/mine"></head><body><p>Genuine article text that belongs to this site alone.</p></body></html>'
        doc = parser.parse(html, url=URL)
        assert doc.canonical_url is None
        assert "canonical_cross_site" in doc.warnings

    def test_lang_disagreement_warns(self, parser):
        html = ('<html lang="en"><body><p>Der schnelle braune Fuchs springt über den '
                'faulen Hund, während der Ausschuss den Quartalsbericht prüft.</p></body></html>').encode()
        doc = parser.parse(html, url=URL)
        assert doc.language.code == "de"
        assert "lang_attr_disagreed" in doc.warnings

    def test_link_cap_warns(self):
        cfg = Config()
        object.__setattr__(cfg.limits, "max_links", 5)
        links = "".join(f'<a href="/p{i}">link number {i}</a>' for i in range(50))
        html = f"<html><body><article><p>Real prose that should survive extraction here.</p>{links}</article></body></html>".encode()
        doc = Parser(cfg).parse(html, url=URL)
        assert len(doc.links) == 5
        assert any(w.startswith("link_cap_reached") for w in doc.warnings)


class TestRobustness:
    def test_try_parse_swallows_rejection(self, parser):
        assert parser.try_parse(b"", url=URL) is None

    def test_empty_document_raises_in_strict_mode(self, parser):
        with pytest.raises(ParseRejected):
            parser.parse(b"   ", url=URL)

    def test_decompression_bomb_rejected(self, parser):
        with pytest.raises(ParseRejected):
            parser.parse(gzip.compress(b"\0" * (40 * 1024 * 1024)), url=URL)

    @pytest.mark.parametrize("payload", [
        b"<html><body><p>" + b"\xff\xfe\x00" * 100 + b"</p></body></html>",
        b"<html><body>" + b"<div>" * 300 + b"deep" + b"</div>" * 300 + b"</body></html>",
        b"<!DOCTYPE html><html><body><p>unclosed<table><tr><td>x</body>",
        "<html><body><p>emoji 🎉 and CJK 日本語テキストです</p></body></html>".encode(),
    ])
    def test_hostile_or_odd_input_does_not_raise(self, parser, payload):
        parser.try_parse(payload, url=URL)  # must not raise

    def test_non_html_parses_but_yields_nothing_indexable(self, parser):
        """lexbor synthesises a <body> for any input, so a "no body" check cannot
        detect non-HTML. Content-type filters it upstream; `is_indexable` catches
        whatever slips through."""
        doc = parser.parse(b"<?xml version='1.0'?><rss><channel/></rss>", url=URL)
        assert doc.is_indexable is False

    def test_gzipped_body_is_decompressed_before_decoding(self):
        """Charset detection on still-compressed bytes produces mojibake."""
        html = ("<html><head><meta charset='utf-8'></head><body><p>The quarterly "
                "report covers the retrieval threshold and block maxima.</p></body></html>")
        doc = Parser(Config()).parse(gzip.compress(html.encode()), url=URL)
        assert "quarterly report" in doc.body


class TestTemplateLearningIntegration:
    def test_repeated_chrome_is_subtracted_after_enough_pages(self):
        cfg = Config()
        parser = Parser(cfg, templates=TemplateLearner(cfg.boilerplate))
        chrome = ("Sign up for our newsletter and receive weekly updates about "
                  "everything happening across the whole of our network today.")

        for i in range(8):
            body = (f"Article number {i} discusses the retrieval threshold and the way "
                    f"block maxima are summed before any posting is decoded at all.")
            html = f"<html><body><main><p>{chrome}</p><p>{body}</p></main></body></html>".encode()
            parser.parse(html, url=f"https://example.com/a{i}")

        final = f"<html><body><main><p>{chrome}</p><p>Unique final article text about block max indexes and thresholds.</p></main></body></html>".encode()
        doc = parser.parse(final, url="https://example.com/final")
        assert "newsletter" not in doc.body
        assert "Unique final article" in doc.body
