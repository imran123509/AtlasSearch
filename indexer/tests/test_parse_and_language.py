from __future__ import annotations

import gzip

import pytest

from atlas_indexer.config import LanguageConfig, ParseLimits
from atlas_indexer.language import identify, normalise_lang_attr
from atlas_indexer.parse import ParseRejected, count_nodes, decompress_capped, safe_parse

EN = ("The quick brown fox jumps over the lazy dog while the committee reviews "
      "the quarterly report and prepares recommendations for the board meeting.")
DE = ("Der schnelle braune Fuchs springt über den faulen Hund, während der "
      "Ausschuss den Quartalsbericht prüft und Empfehlungen vorbereitet.")
FR = ("Le rapide renard brun saute par-dessus le chien paresseux pendant que le "
      "comité examine le rapport trimestriel et prépare ses recommandations.")


class TestSafeParse:
    def test_basic_document(self):
        out = safe_parse(b"", ParseLimits(), text="<html><body><p>hi there</p></body></html>")
        assert out.tree.body is not None
        assert out.node_count > 0

    def test_empty_document_rejected(self):
        with pytest.raises(ParseRejected, match="empty_document"):
            safe_parse(b"", ParseLimits(), text="   \n  ")

    def test_broken_html_still_parses(self):
        """Error-tolerant tree building. Real HTML is broken; that is not an error."""
        out = safe_parse(b"", ParseLimits(), text="<html><body><p>unclosed<div><b>x</body>")
        assert "unclosed" in out.tree.body.text()

    def test_node_cap_rejects_absurd_documents(self):
        html = "<html><body>" + "<div>" * 2000 + "x" + "</div>" * 2000 + "</body></html>"
        with pytest.raises(ParseRejected, match="node_cap_exceeded"):
            safe_parse(b"", ParseLimits(max_nodes=500), text=html)

    def test_oversize_text_is_truncated_not_rejected(self):
        out = safe_parse(b"", ParseLimits(max_bytes=500), text="<html><body>" + "a" * 5000)
        assert out.truncated is True

    def test_raw_bytes_path_decodes(self):
        out = safe_parse(b"<html><body><p>bytes path</p></body></html>", ParseLimits())
        assert "bytes path" in out.tree.body.text()


class TestDecompression:
    def test_plain_bytes_pass_through(self):
        assert decompress_capped(b"<html>x</html>", ParseLimits()) == b"<html>x</html>"

    def test_gzip_inflated(self):
        raw = gzip.compress(b"<html><body>inflated</body></html>")
        assert b"inflated" in decompress_capped(raw, ParseLimits())

    def test_decompression_bomb_rejected(self):
        """A few hundred KB that expands to gigabytes is how a parse worker gets
        OOM-killed. Counting output as it inflates is the only defence."""
        bomb = gzip.compress(b"\0" * (50 * 1024 * 1024))
        with pytest.raises(ParseRejected, match="decompression_bomb"):
            decompress_capped(bomb, ParseLimits(max_bytes=1024 * 1024))

    def test_corrupt_gzip_rejected_not_raised_raw(self):
        with pytest.raises(ParseRejected, match="decompress_failed"):
            decompress_capped(b"\x1f\x8b" + b"garbage" * 100, ParseLimits())


class TestNodeCounting:
    def test_counts_elements_and_text(self):
        from selectolax.lexbor import LexborHTMLParser

        n, over = count_nodes(LexborHTMLParser("<html><body><p>a</p><p>b</p></body></html>"), 1000)
        assert n > 3 and over is False

    def test_bails_at_the_cap(self):
        from selectolax.lexbor import LexborHTMLParser

        html = "<html><body>" + "<p>x</p>" * 500 + "</body></html>"
        n, over = count_nodes(LexborHTMLParser(html), 50)
        assert over is True and n == 51  # stopped early, did not walk the rest


class TestLanguage:
    @pytest.fixture
    def lcfg(self):
        return LanguageConfig()

    @pytest.mark.parametrize("text,expected", [(EN, "en"), (DE, "de"), (FR, "fr")])
    def test_identification(self, lcfg, text, expected):
        result = identify(text, lcfg)
        assert result.code == expected
        assert result.source == "model"
        assert result.confidence > 0.5

    def test_confident_model_overrides_a_wrong_lang_attribute(self, lcfg):
        """The attribute is a prior, never truth — it is wrong often enough to
        matter, and tokenisation branches on this."""
        result = identify(DE, lcfg, lang_attr="en")
        assert result.code == "de"
        assert result.source == "model"
        assert result.disagreed is True

    def test_short_text_falls_back_to_the_attribute(self, lcfg):
        result = identify("Bonjour", lcfg, lang_attr="fr-CA")
        assert result.code == "fr"
        assert result.source == "attribute"

    def test_short_text_with_no_attribute_is_undetermined(self, lcfg):
        result = identify("hi", lcfg)
        assert result.code == "und"
        assert result.confidence == 0.0

    def test_agreement_is_not_flagged_as_disagreement(self, lcfg):
        assert identify(EN, lcfg, lang_attr="en-US").disagreed is False

    def test_empty_text(self, lcfg):
        assert identify("", lcfg).code == "und"


class TestLangAttrNormalisation:
    @pytest.mark.parametrize("raw,expected", [
        ("en", "en"), ("en-GB", "en"), ("EN_us", "en"), ("  fr-CA  ", "fr"), ("zh-Hant", "zh"),
    ])
    def test_normalised(self, raw, expected):
        assert normalise_lang_attr(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "{{lang}}", "123", "x" * 10])
    def test_junk_rejected(self, raw):
        assert normalise_lang_attr(raw) is None
