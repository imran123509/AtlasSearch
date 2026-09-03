"""URL canonicalisation.

Implements the fixed ordering from features/URL-DE-DUPLICATION.md. The order
matters: reordering these steps produces different output for the same input,
which defeats the purpose.

Must be a pure function — no clocks, no network, no map-iteration-order
dependence. `canonicalise(canonicalise(u)) == canonicalise(u)` is property-tested.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import tldextract

# Public-suffix data is bundled and read from disk; no network lookup at runtime.
_extract = tldextract.TLDExtract(suffix_list_urls=())

# RFC 3986 unreserved characters: safe to percent-decode.
_UNRESERVED = re.compile(r"%(2D|2E|5F|7E|3[0-9]|[46][1-9A-F]|[57][0-9A]|4[0-9A-F])", re.I)
_PCT = re.compile(r"%([0-9a-fA-F]{2})")

DEFAULT_PORTS = {"http": 80, "https": 443}

# Junk parameters are removed from an explicit ALLOW-LIST of known-junk names.
# Never invert this: "strip everything not known to be meaningful" silently
# destroys real content (?page=2, ?id=447).
TRACKING_PARAMS = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "utm_id", "utm_source_platform", "utm_creative_format",
        "gclid", "gclsrc", "dclid", "gbraid", "wbraid",
        "fbclid", "msclkid", "twclid", "igshid", "ttclid",
        "mc_cid", "mc_eid", "_ga", "_gl", "yclid", "vero_id",
        "ref", "referrer", "source", "campaign_id",
        "at_medium", "at_campaign", "cmpid", "ncid", "sr_share",
        "spm", "scm", "share_token",
    }
)

SESSION_PARAMS = frozenset(
    {"phpsessid", "jsessionid", "aspsessionid", "sid", "sessionid", "session_id", "zenid"}
)


def _normalise_percent_encoding(s: str) -> str:
    """Decode unreserved octets, uppercase the hex of everything else.

    Reserved characters (%2F, %3F, ...) are left encoded — decoding them would
    change the URL's structure.
    """

    def _decode_unreserved(m: re.Match[str]) -> str:
        return unquote(m.group(0))

    def _upper_hex(m: re.Match[str]) -> str:
        return "%" + m.group(1).upper()

    return _PCT.sub(_upper_hex, _UNRESERVED.sub(_decode_unreserved, s))


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 §5.2.4."""
    out: list[str] = []
    for seg in path.split("/"):
        if seg == ".":
            continue
        if seg == "..":
            if out and out[-1] != "":
                out.pop()
            continue
        out.append(seg)
    result = "/".join(out)
    if path.startswith("/") and not result.startswith("/"):
        result = "/" + result
    return result or "/"


def _normalise_host(host: str) -> str:
    host = host.strip().rstrip(".").lower()
    if not host:
        return host
    try:
        # IDNA: münchen.de -> xn--mnchen-3ya.de
        return host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return host


def canonicalise(url: str, *, base: str | None = None) -> str:
    """Return the canonical form of `url`, optionally resolved against `base`."""
    if base:
        from urllib.parse import urljoin

        url = urljoin(base, url)

    parts = urlsplit(url.strip())

    # 1-2. scheme + host lowercase, drop default port
    scheme = parts.scheme.lower()
    host = _normalise_host(parts.hostname or "")
    port = parts.port
    netloc = host
    if port is not None and DEFAULT_PORTS.get(scheme) != port:
        netloc = f"{host}:{port}"

    # Credentials in URLs are never kept — they are not part of a document's identity.

    # 4-5. percent-encoding, then dot segments
    path = _normalise_percent_encoding(parts.path)
    path = _remove_dot_segments(path)
    # Re-encode any raw characters that must be escaped, without touching '%'.
    path = quote(path, safe="/%:@!$&'()*+,;=~-._")

    # 7-9. filter and sort the query
    query = ""
    if parts.query:
        pairs = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS and k.lower() not in SESSION_PARAMS
        ]
        pairs.sort(key=lambda kv: (kv[0], kv[1]))
        query = urlencode(pairs, doseq=False)

    # 10. drop a trailing slash on paths that look like files, keep it on directories
    if len(path) > 1 and path.endswith("/") and "." in path.rsplit("/", 2)[-2:][0]:
        path = path.rstrip("/")

    # 6. fragment is always dropped
    return urlunsplit((scheme, netloc, path, query, ""))


def registrable_domain(url_or_host: str) -> str:
    """Public-suffix-aware registrable domain: 'a.b.example.co.uk' -> 'example.co.uk'.

    This — not the hostname — is the politeness key, so that a thousand
    subdomains of one site do not each get their own budget.
    """
    host = url_or_host
    if "://" in url_or_host:
        host = urlsplit(url_or_host).hostname or ""
    ext = _extract(host)
    if not ext.domain:
        return host.lower()
    # tldextract >=6 renamed `registered_domain`; read the new name if present so
    # we never touch the deprecated property.
    if hasattr(type(ext), "top_domain_under_public_suffix"):
        registered = ext.top_domain_under_public_suffix
    else:  # pragma: no cover - older tldextract
        registered = ext.registered_domain
    return (registered or host).lower()


def same_site(a: str, b: str) -> bool:
    return registrable_domain(a) == registrable_domain(b)


def is_crawlable_scheme(url: str) -> bool:
    return urlsplit(url).scheme in ("http", "https")
