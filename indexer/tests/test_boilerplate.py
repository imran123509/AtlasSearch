from __future__ import annotations

import pytest
from selectolax.lexbor import LexborHTMLParser

from atlas_indexer.boilerplate import (
    TemplateLearner,
    build_blocks,
    classify,
    extract,
)
from atlas_indexer.config import BoilerplateConfig


@pytest.fixture
def bcfg():
    return BoilerplateConfig()


def _body(html: str):
    return LexborHTMLParser(html).body


PROSE = (
    "The retrieval loop keeps a running threshold equal to the score of the current "
    "kth best result and sums the per-term block maxima before decoding anything."
)


class TestExtraction:
    def test_navigation_and_footer_are_removed(self, bcfg, article):
        result = extract(_body(article.decode()), bcfg)
        assert "Privacy" not in result.text
        assert "Alpha" not in result.text
        assert "Related one" not in result.text

    def test_article_body_is_kept(self, bcfg, article):
        result = extract(_body(article.decode()), bcfg)
        assert "running threshold" in result.text
        assert "prunes harder" in result.text

    def test_headings_are_kept_despite_being_short(self, bcfg, article):
        result = extract(_body(article.decode()), bcfg)
        assert "Why the feedback matters" in result.text

    def test_scripts_and_styles_never_contribute_text(self, bcfg):
        html = f"<body><script>var x='SECRET'</script><style>.a{{b:'CSS'}}</style><p>{PROSE}</p></body>"
        result = extract(_body(html), bcfg)
        assert "SECRET" not in result.text and "CSS" not in result.text

    def test_a_link_list_is_boilerplate(self, bcfg):
        links = " ".join(f'<a href="/{i}">Link number {i}</a>' for i in range(20))
        result = extract(_body(f"<body><div>{links}</div><p>{PROSE}</p></body>"), bcfg)
        assert "Link number" not in result.text
        assert "running threshold" in result.text

    def test_chrome_class_names_are_dropped(self, bcfg):
        html = (
            f'<body><div class="cookie-consent">We use cookies to improve your experience '
            f'on this website and to show relevant advertising to you.</div>'
            f"<p>{PROSE}</p></body>"
        )
        result = extract(_body(html), bcfg)
        assert "cookies" not in result.text

    def test_content_class_survives_a_chrome_lookalike_word(self, bcfg):
        html = f'<body><div class="article-content related-articles"><p>{PROSE}</p></div></body>'
        result = extract(_body(html), bcfg)
        assert "running threshold" in result.text


class TestDiagnostics:
    def test_retained_ratio_is_reported(self, bcfg, article):
        result = extract(_body(article.decode()), bcfg)
        assert 0.0 < result.retained_ratio <= 1.0
        assert result.visible_chars > 0

    def test_over_removal_warns_and_falls_back(self, bcfg):
        """The failure the doc singles out: the remover ate the article and
        nothing else in the system would notice."""
        # Prose wrapped entirely in an element the structural pass drops.
        html = f'<body><nav>{"".join(f"<p>{PROSE}</p>" for _ in range(3))}</nav></body>'
        result = extract(_body(html), bcfg)
        assert "suspiciously_low_retention" in result.warnings
        assert "used_fallback_extraction" in result.warnings
        assert "running threshold" in result.text, "fallback did not recover the content"

    def test_a_genuinely_empty_page_does_not_warn_about_retention(self, bcfg):
        result = extract(_body("<body><nav><a href='/x'>Home</a></nav></body>"), bcfg)
        assert "suspiciously_low_retention" not in result.warnings


class TestBlockFeatures:
    def test_link_density_is_computed(self, bcfg):
        blocks = build_blocks(_body('<body><p>one two <a href="/x">three four</a></p></body>'), bcfg)
        assert blocks[0].num_words == 4
        assert blocks[0].link_words == 2
        assert blocks[0].link_density == pytest.approx(0.5)

    def test_blocks_split_at_block_boundaries(self, bcfg):
        blocks = build_blocks(_body("<body><p>first block here</p><p>second block here</p></body>"), bcfg)
        assert len(blocks) == 2

    def test_inline_elements_do_not_split_a_block(self, bcfg):
        blocks = build_blocks(_body("<body><p>one <em>two</em> three four</p></body>"), bcfg)
        assert len(blocks) == 1
        assert blocks[0].num_words == 4

    def test_br_splits_a_block(self, bcfg):
        blocks = build_blocks(_body("<body><p>first line here<br>second line here</p></body>"), bcfg)
        assert len(blocks) == 2

    def test_very_short_blocks_are_dropped(self, bcfg):
        assert build_blocks(_body("<body><p>hi</p></body>"), bcfg) == []

    def test_classify_marks_long_prose_as_content(self, bcfg):
        blocks = classify(build_blocks(_body(f"<body><p>{PROSE}</p></body>"), bcfg), bcfg)
        assert blocks[0].is_content
        assert blocks[0].reason == "long"

    def test_classify_marks_high_link_density_as_boilerplate(self, bcfg):
        html = '<body><p><a href="/a">alpha beta</a> <a href="/b">gamma delta</a></p></body>'
        blocks = classify(build_blocks(_body(html), bcfg), bcfg)
        assert not blocks[0].is_content
        assert blocks[0].reason == "link_density"


class TestTemplateLearning:
    def test_repeated_blocks_become_template_after_enough_pages(self, bcfg):
        learner = TemplateLearner(bcfg)
        shared = "Subscribe to our newsletter for weekly updates and special offers today"
        for i in range(6):
            html = f"<body><p>{shared}</p><p>{PROSE} Page number {i} has unique text here.</p></body>"
            learner.observe("example.com", build_blocks(_body(html), bcfg))

        assert learner.is_template("example.com", shared)
        assert not learner.is_template("example.com", f"{PROSE} Page number 0 has unique text here.")

    def test_below_the_page_threshold_nothing_is_template(self, bcfg):
        learner = TemplateLearner(bcfg)
        shared = "Subscribe to our newsletter for weekly updates and special offers today"
        for _ in range(2):
            learner.observe("example.com", build_blocks(_body(f"<body><p>{shared}</p></body>"), bcfg))
        assert not learner.is_template("example.com", shared)

    def test_subtract_removes_template_blocks_from_content(self, bcfg):
        learner = TemplateLearner(bcfg)
        shared = "Subscribe to our newsletter for weekly updates and special offers today"
        for i in range(6):
            html = f"<body><p>{shared}</p><p>{PROSE} Unique body text number {i} here.</p></body>"
            learner.observe("example.com", build_blocks(_body(html), bcfg))

        final = f"<body><p>{shared}</p><p>{PROSE} Unique body text number 99 here.</p></body>"
        result = extract(_body(final), bcfg, host="example.com", templates=learner)
        assert "Subscribe to our newsletter" not in result.text
        assert "Unique body text number 99" in result.text

    def test_hosts_are_learned_independently(self, bcfg):
        learner = TemplateLearner(bcfg)
        shared = "Shared chrome text that repeats across every page of this website"
        for _ in range(6):
            learner.observe("a.test", build_blocks(_body(f"<body><p>{shared}</p></body>"), bcfg))
        assert learner.is_template("a.test", shared)
        assert not learner.is_template("b.test", shared)
