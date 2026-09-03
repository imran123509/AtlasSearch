from __future__ import annotations

from selectolax.lexbor import LexborHTMLParser as P

from atlas_indexer.config import ParseLimits
from atlas_indexer.links import (
    authority_edges,
    discovery_urls,
    extract_links,
    validate_canonical,
)
from atlas_indexer.models import LinkRel

SRC = "https://example.com/blog/post"
LIMITS = ParseLimits()


def _links(html: str, *, source=SRC, base=None, limits=LIMITS):
    links, warnings = extract_links(P(html), source_url=source, limits=limits, base_href=base)
    return links, warnings


class TestResolution:
    def test_relative_resolved_against_document(self):
        links, _ = _links('<a href="../other">x</a>')
        assert links[0].target_url == "https://example.com/other"

    def test_base_href_changes_resolution(self):
        """Missing <base href> silently produces wrong URLs for the whole page."""
        links, _ = _links('<a href="img">x</a>', base="https://cdn.test/assets/")
        assert links[0].target_url == "https://cdn.test/assets/img"

    def test_relative_base_resolved_against_document_first(self):
        links, _ = _links('<a href="x">t</a>', base="/root/")
        assert links[0].target_url == "https://example.com/root/x"

    def test_targets_are_canonicalised(self):
        links, _ = _links('<a href="/p?utm_source=nl&id=4#frag">x</a>')
        assert links[0].target_url == "https://example.com/p?id=4"

    def test_protocol_relative(self):
        links, _ = _links('<a href="//other.test/x">t</a>')
        assert links[0].target_url == "https://other.test/x"

    def test_self_link_dropped(self):
        assert _links(f'<a href="{SRC}">self</a>')[0] == []


class TestFiltering:
    def test_non_document_schemes_skipped(self):
        html = ('<a href="javascript:void(0)">a</a><a href="mailto:x@y.z">b</a>'
                '<a href="tel:+1">c</a><a href="#frag">d</a><a href="data:text/html,x">e</a>')
        assert _links(html)[0] == []

    def test_duplicate_target_and_anchor_collapsed(self):
        links, _ = _links('<a href="/x">same</a><a href="/x">same</a>')
        assert len(links) == 1

    def test_same_target_different_anchor_both_kept(self):
        """Both anchors describe the target; anchor text is the point."""
        links, _ = _links('<a href="/x">first text</a><a href="/x">second text</a>')
        assert len(links) == 2

    def test_link_cap_warns(self):
        limits = ParseLimits(max_links=10)
        html = "".join(f'<a href="/p{i}">link {i}</a>' for i in range(50))
        links, warnings = _links(html, limits=limits)
        assert len(links) == 10
        assert any(w.startswith("link_cap_reached") for w in warnings)

    def test_malformed_href_does_not_raise(self):
        links, _ = _links('<a href="ht tp://bad url">x</a><a href="/good">y</a>')
        assert any(link.target_url.endswith("/good") for link in links)


class TestClassification:
    def test_rel_nofollow(self):
        assert _links('<a href="/x" rel="nofollow">t</a>')[0][0].rel is LinkRel.NOFOLLOW

    def test_rel_ugc(self):
        assert _links('<a href="/x" rel="ugc">t</a>')[0][0].rel is LinkRel.UGC

    def test_rel_sponsored_wins_over_nofollow(self):
        assert _links('<a href="/x" rel="nofollow sponsored">t</a>')[0][0].rel is LinkRel.SPONSORED

    def test_default_is_follow(self):
        assert _links('<a href="/x">t</a>')[0][0].rel is LinkRel.FOLLOW

    def test_internal_vs_external_by_registrable_domain(self):
        links, _ = _links('<a href="https://sub.example.com/a">i</a><a href="https://other.test/b">e</a>')
        by_host = {link.target_url: link.internal for link in links}
        assert by_host["https://sub.example.com/a"] is True
        assert by_host["https://other.test/b"] is False

    def test_links_in_chrome_are_marked(self):
        html = '<body><nav><a href="/n">nav</a></nav><article><a href="/c">content</a></article></body>'
        links, _ = _links(html)
        marks = {link.target_url[-2:]: link.in_main_content for link in links}
        assert marks["/n"] is False
        assert marks["/c"] is True


class TestAnchorContext:
    def test_surrounding_text_captured(self):
        """'click here' is useless; the sentence around it is not."""
        html = "<p>Before the anchor we say something and then <a href='/x'>click here</a> and continue afterwards.</p>"
        links, _ = _links(html)
        assert "click here" in links[0].context
        assert "something" in links[0].context or "continue" in links[0].context

    def test_context_bounded_by_window(self):
        limits = ParseLimits(anchor_context_chars=10)
        html = f"<p>{'a' * 500} <a href='/x'>anchor</a> {'b' * 500}</p>"
        links, _ = _links(html, limits=limits)
        assert len(links[0].context) <= 10 + len("anchor") + 10 + 2

    def test_anchor_text_truncated(self):
        limits = ParseLimits(max_anchor_chars=20)
        links, _ = _links(f'<a href="/x">{"word " * 100}</a>', limits=limits)
        assert len(links[0].anchor_text) <= 20

    def test_whitespace_normalised(self):
        links, _ = _links('<a href="/x">  spread \n out  </a>')
        assert links[0].anchor_text == "spread out"


class TestGraphViews:
    def test_discovery_includes_nofollow(self):
        """nofollow means 'I do not vouch for this', not 'do not visit this'."""
        links, _ = _links('<a href="/a">a</a><a href="/b" rel="nofollow">b</a>')
        urls = discovery_urls(links)
        assert len(urls) == 2

    def test_authority_excludes_nofollow_ugc_sponsored(self):
        html = ('<a href="/a">a</a><a href="/b" rel="nofollow">b</a>'
                '<a href="/c" rel="ugc">c</a><a href="/d" rel="sponsored">d</a>')
        links, _ = _links(html)
        assert [link.target_url for link in authority_edges(links)] == ["https://example.com/a"]


class TestCanonicalValidation:
    def test_same_site_accepted(self):
        target, warning = validate_canonical("/canonical-path", SRC)
        assert target == "https://example.com/canonical-path"
        assert warning is None

    def test_cross_site_rejected_as_spam_signal(self):
        """A cross-site canonical is a ranking-hijack primitive."""
        target, warning = validate_canonical("https://attacker.test/mine", SRC)
        assert target is None
        assert warning == "canonical_cross_site"

    def test_subdomain_is_same_site(self):
        target, _ = validate_canonical("https://www.example.com/x", SRC)
        assert target == "https://www.example.com/x"

    def test_absent(self):
        assert validate_canonical(None, SRC) == (None, None)
