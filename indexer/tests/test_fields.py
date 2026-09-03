from __future__ import annotations

from selectolax.lexbor import LexborHTMLParser as P

from atlas_indexer import fields as F


class TestTitle:
    def test_title_tag_preferred(self):
        assert F.extract_title(P("<html><head><title>Real Title</title></head><body><h1>H</h1></body>")) == "Real Title"

    def test_og_title_before_h1(self):
        """og:title is a deliberate statement of the document's name; an <h1>
        is often site branding."""
        html = '<html><head><meta property="og:title" content="OG Title"></head><body><h1>Site Name</h1></body>'
        assert F.extract_title(P(html)) == "OG Title"

    def test_h1_as_last_resort(self):
        assert F.extract_title(P("<html><body><h1>Only Heading</h1></body>")) == "Only Heading"

    def test_whitespace_collapsed(self):
        assert F.extract_title(P("<title>  spaced \n out  </title>")) == "spaced out"

    def test_missing_title_is_empty_not_an_error(self):
        assert F.extract_title(P("<html><body><p>x</p></body>")) == ""


class TestHeadings:
    def test_h1_h2_h3_collected_in_order(self):
        html = "<body><h1>One</h1><h2>Two</h2><h3>Three</h3><h4>Four</h4></body>"
        assert F.extract_headings(P(html)) == ["One", "Two", "Three"]

    def test_absurdly_long_heading_skipped(self):
        assert F.extract_headings(P(f"<body><h1>{'x' * 400}</h1></body>")) == []

    def test_limit_respected(self):
        html = "<body>" + "".join(f"<h2>Heading {i}</h2>" for i in range(100)) + "</body>"
        assert len(F.extract_headings(P(html), limit=10)) == 10


class TestMetaDescription:
    def test_name_description(self):
        assert F.extract_meta_description(P('<meta name="description" content="D">')) == "D"

    def test_og_fallback(self):
        assert F.extract_meta_description(P('<meta property="og:description" content="OG">')) == "OG"

    def test_absent(self):
        assert F.extract_meta_description(P("<html><body></body>")) == ""


class TestRobotsMeta:
    def test_noindex_parsed(self):
        assert "noindex" in F.extract_robots_meta(P('<meta name="robots" content="noindex">'))

    def test_multiple_directives(self):
        got = F.extract_robots_meta(P('<meta name="robots" content="noarchive, nosnippet">'))
        assert got == {"noarchive", "nosnippet"}

    def test_none_expands_to_noindex_nofollow(self):
        got = F.extract_robots_meta(P('<meta name="robots" content="none">'))
        assert {"noindex", "nofollow"} <= got

    def test_agent_specific_tag_is_honoured(self):
        got = F.extract_robots_meta(P('<meta name="atlassearchbot" content="noindex">'))
        assert "noindex" in got

    def test_another_agents_tag_is_ignored(self):
        got = F.extract_robots_meta(P('<meta name="googlebot" content="noindex">'))
        assert "noindex" not in got

    def test_unknown_directives_dropped(self):
        assert F.extract_robots_meta(P('<meta name="robots" content="max-snippet:-1">')) == frozenset()


class TestStructuredData:
    def test_json_ld_parsed(self):
        html = '<script type="application/ld+json">{"@type":"Article","headline":"H"}</script>'
        assert F.extract_structured_data(P(html))[0]["headline"] == "H"

    def test_array_flattened(self):
        html = '<script type="application/ld+json">[{"a":1},{"b":2}]</script>'
        assert len(F.extract_structured_data(P(html))) == 2

    def test_malformed_json_is_skipped_not_raised(self):
        """Malformed JSON-LD is extremely common; it is not an error."""
        html = '<script type="application/ld+json">{not json at all,,}</script>'
        assert F.extract_structured_data(P(html)) == []

    def test_oversized_block_skipped(self):
        html = f'<script type="application/ld+json">{{"a":"{"x" * 300_000}"}}</script>'
        assert F.extract_structured_data(P(html)) == []


class TestPublished:
    def test_from_json_ld(self):
        data = [{"datePublished": "2026-03-14"}]
        assert F.extract_published(P("<html></html>"), data) == "2026-03-14"

    def test_from_article_meta(self):
        html = '<meta property="article:published_time" content="2026-01-02T03:04:05Z">'
        assert F.extract_published(P(html), []) == "2026-01-02T03:04:05Z"

    def test_from_time_element(self):
        assert F.extract_published(P('<time datetime="2026-05-05">May</time>'), []) == "2026-05-05"

    def test_absent(self):
        assert F.extract_published(P("<html></html>"), []) is None


class TestUrlTokenisation:
    def test_path_becomes_words(self):
        got = F.tokenise_url("https://example.com/blog/2026/block-max-wand.html")
        assert "block" in got and "max" in got and "wand" in got

    def test_www_stripped_and_host_included(self):
        assert F.tokenise_url("https://www.example.com/docs/yy") == "example com docs yy"

    def test_single_character_segments_dropped(self):
        """Deliberate: `url_text` is a low-weight field, so noise costs more than
        the rare meaningful one-letter segment (`/r/`, `/c/`)."""
        assert F.tokenise_url("https://example.com/a/b/reference") == "example com reference"

    def test_extension_words_dropped(self):
        assert "html" not in F.tokenise_url("https://e.com/a/page.html")

    def test_opaque_hex_ids_dropped(self):
        got = F.tokenise_url("https://e.com/p/8f2a91c4de99bb01/title-here")
        assert "8f2a91c4de99bb01" not in got
        assert "title" in got

    def test_camel_case_split(self):
        assert "block" in F.tokenise_url("https://e.com/blockMaxWand")

    def test_percent_encoding_decoded(self):
        assert "hello world" in F.tokenise_url("https://e.com/hello%20world")


class TestBaseAndLang:
    def test_base_href_read(self):
        assert F.extract_base_href(P('<base href="https://cdn.test/x/">')) == "https://cdn.test/x/"

    def test_base_absent(self):
        assert F.extract_base_href(P("<html></html>")) is None

    def test_lang_attr_read(self):
        assert F.extract_lang_attr(P('<html lang="en-GB"></html>')) == "en-GB"

    def test_lang_absent(self):
        assert F.extract_lang_attr(P("<html></html>")) is None


class TestCanonical:
    def test_read(self):
        assert F.extract_canonical(P('<link rel="canonical" href="https://e.com/x">')) == "https://e.com/x"

    def test_multivalued_rel(self):
        assert F.extract_canonical(P('<link rel="alternate canonical" href="/y">')) == "/y"

    def test_absent(self):
        assert F.extract_canonical(P("<html></html>")) is None
