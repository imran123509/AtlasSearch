from __future__ import annotations

import pytest

from atlas_crawler.urlnorm import canonicalise, registrable_domain, same_site

CORPUS = [
    "HTTP://Example.COM:80/a/./b/../c?utm_source=x&id=447#frag",
    "https://example.com/",
    "https://example.com/path/",
    "https://example.com/file.html/",
    "https://user:pw@example.com/x",
    "https://münchen.de/straße",
    "http://example.com/%7Euser/%2Fencoded",
    "https://a.b.example.co.uk/deep/path?b=2&a=1",
    "https://example.com/search?q=hello+world&page=2",
    "https://example.com/x?PHPSESSID=abc&real=1",
]


@pytest.mark.parametrize("url", CORPUS)
def test_canonicalise_is_idempotent(url):
    """canon(canon(u)) == canon(u).

    Property-tested because a non-idempotent canonicaliser fingerprints the same
    URL differently on re-discovery, which silently defeats de-duplication.
    """
    once = canonicalise(url)
    assert canonicalise(once) == once


@pytest.mark.parametrize("url", CORPUS)
def test_canonicalise_is_deterministic(url):
    assert canonicalise(url) == canonicalise(url)


def test_scheme_host_lowercased_default_port_dropped():
    assert canonicalise("HTTP://Example.COM:80/a") == "http://example.com/a"
    assert canonicalise("https://Example.com:443/a") == "https://example.com/a"


def test_nondefault_port_kept():
    assert canonicalise("http://example.com:8080/a") == "http://example.com:8080/a"


def test_fragment_dropped():
    assert canonicalise("https://example.com/p#section") == "https://example.com/p"


def test_dot_segments_resolved():
    assert canonicalise("https://example.com/a/./b/../c") == "https://example.com/a/c"


def test_tracking_params_stripped_real_params_kept():
    out = canonicalise("https://example.com/p?utm_source=nl&fbclid=z&id=447&page=2")
    assert "utm_source" not in out and "fbclid" not in out
    # Meaningful parameters must survive — stripping unknown params silently
    # destroys real content.
    assert "id=447" in out and "page=2" in out


def test_session_params_stripped():
    assert "PHPSESSID" not in canonicalise("https://example.com/x?PHPSESSID=abc&real=1")
    assert "real=1" in canonicalise("https://example.com/x?PHPSESSID=abc&real=1")


def test_query_params_sorted():
    a = canonicalise("https://example.com/p?b=2&a=1")
    b = canonicalise("https://example.com/p?a=1&b=2")
    assert a == b


def test_unreserved_percent_decoded_reserved_left_alone():
    out = canonicalise("http://example.com/%7Euser/%2fencoded")
    assert "~user" in out
    assert "%2F" in out  # reserved: uppercased, not decoded


def test_idn_punycoded():
    assert canonicalise("https://münchen.de/x").startswith("https://xn--mnchen-3ya.de")


def test_credentials_dropped():
    assert "user" not in canonicalise("https://user:pw@example.com/x").split("/x")[0]


def test_relative_resolution_against_base():
    assert canonicalise("../b", base="https://example.com/a/c/d") == "https://example.com/a/b"


class TestRegistrableDomain:
    def test_simple(self):
        assert registrable_domain("https://www.example.com/x") == "example.com"

    def test_multipart_public_suffix(self):
        # The reason we use a PSL library rather than "last two labels".
        assert registrable_domain("https://a.b.example.co.uk/x") == "example.co.uk"

    def test_deep_subdomain(self):
        assert registrable_domain("https://a.b.c.d.example.com/") == "example.com"

    def test_bare_host(self):
        assert registrable_domain("shop.example.org") == "example.org"

    def test_same_site(self):
        assert same_site("https://a.example.com/1", "https://b.example.com/2")
        assert not same_site("https://example.com/1", "https://example.org/2")
