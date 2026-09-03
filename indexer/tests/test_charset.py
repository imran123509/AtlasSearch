from __future__ import annotations

import codecs

import pytest

from atlas_indexer.charset import decode, detect, normalise_label


class TestPrecedence:
    def test_bom_beats_a_contradicting_header(self):
        """A BOM is a fact about the bytes; a header is a claim about them.

        features/HTML-PARSER.md lists the cascade as header → meta → BOM. That
        ordering is wrong and this test pins the corrected behaviour.
        """
        raw = codecs.BOM_UTF8 + "<html><p>café</p>".encode()
        result = detect(raw, content_type="text/html; charset=iso-8859-1")
        assert result.encoding == "utf-8"
        assert result.source == "bom"

    def test_bom_beats_a_contradicting_meta(self):
        raw = codecs.BOM_UTF8 + b'<html><meta charset="shift_jis"><p>x</p>'
        assert detect(raw).source == "bom"

    def test_header_beats_meta(self):
        raw = b'<html><head><meta charset="windows-1251"></head><body>abc</body></html>'
        result = detect(raw, content_type="text/html; charset=utf-8")
        assert result.encoding == "utf-8"
        assert result.source == "header"

    def test_meta_used_when_no_header(self):
        raw = '<html><head><meta charset="utf-8"></head><body>café</body></html>'.encode()
        result = detect(raw)
        assert result.encoding == "utf-8"
        assert result.source == "meta"

    def test_statistical_when_nothing_declared(self):
        raw = "Der schnelle braune Fuchs springt über den faulen Hund.".encode("utf-8") * 20
        assert detect(raw).source in ("statistical", "fallback")

    def test_fallback_always_terminates(self):
        assert detect(b"\xff\xfe\x00\x01\x02garbage\x99\x9a").encoding is not None


class TestDeclarationValidation:
    def test_a_lying_utf8_declaration_is_rejected(self):
        """Pages declaring utf-8 while emitting cp1252 are common.

        Trusting the declaration is how mojibake reaches the index.
        """
        raw = b'<html><meta charset="utf-8"><body>caf\xe9 na\xefve</body></html>'
        result = detect(raw)
        assert result.source != "meta", "trusted a declaration the bytes contradict"

    def test_a_truthful_declaration_is_kept(self):
        raw = '<html><meta charset="utf-8"><body>café naïve</body></html>'.encode("utf-8")
        assert detect(raw).source == "meta"


class TestAliases:
    @pytest.mark.parametrize(
        "label", ["iso-8859-1", "ISO8859-1", "latin1", "Latin-1", "ascii", "us-ascii"]
    )
    def test_latin1_family_maps_to_windows_1252(self, label):
        """HTML5 mandates this. Honouring iso-8859-1 literally turns every smart
        quote and em dash into a replacement character."""
        assert normalise_label(label) == "windows-1252"

    def test_gb2312_maps_to_gbk(self):
        assert normalise_label("gb2312") == "gbk"

    def test_unknown_label_returns_none(self):
        assert normalise_label("not-a-real-encoding") is None

    def test_quoted_label_is_stripped(self):
        assert normalise_label('"utf-8"') == "utf-8"

    def test_none_and_empty(self):
        assert normalise_label(None) is None
        assert normalise_label("") is None


class TestDecode:
    def test_bom_is_stripped_from_output(self):
        text, _ = decode(codecs.BOM_UTF8 + b"<p>hello</p>")
        assert not text.startswith("﻿")
        assert text == "<p>hello</p>"

    def test_cp1252_smart_quotes_survive(self):
        raw = b"<p>\x93quoted\x94 \x97 dash</p>"
        text, result = decode(raw, content_type="text/html; charset=iso-8859-1")
        assert "“" in text and "”" in text
        assert result.encoding == "windows-1252"

    def test_undecodable_bytes_become_replacement_not_an_exception(self):
        text, _ = decode(b"<p>\xff\xfe\x00bad</p>")
        assert isinstance(text, str)

    def test_utf16_roundtrip(self):
        raw = codecs.BOM_UTF16_LE + "<p>hello</p>".encode("utf-16-le")
        text, result = decode(raw)
        assert result.encoding == "utf-16-le"
        assert text == "<p>hello</p>"

    def test_empty_input(self):
        text, result = decode(b"")
        assert text == ""
        assert result.encoding
