"""The render decision.

Static parse is ~1 ms of CPU. A headless render is ~600 ms wall and ~1.2 CPU
seconds — roughly 500x. That sounds prohibitive and the arithmetic says otherwise:
25% of 400M full fetches/day is 1,160 renders/s, which at 0.6 s each is ~700
concurrent browsers and ~1,400 cores — about 40 machines against a 27,000-host
fleet. **Render more than instinct suggests.**

Where this actually hurts is the classifier below. When it says no on a site that
needed rendering, that site's content is silently absent from the index: no error,
no alert, just missing documents. So it is instrumented as a quality surface, and
the audit path is not optional.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date

from .config import RenderConfig
from .models import RenderDecision

_SCRIPT_BLOCK = re.compile(rb"<script\b[^>]*>(.*?)</script\s*>", re.I | re.S)
_SCRIPT_SRC = re.compile(rb"<script\b[^>]*\bsrc\s*=", re.I)


def script_weight(raw: bytes) -> int:
    """Approximate JS payload: inline bytes plus a nominal cost per external file.

    External scripts are the ones that usually build the page, but we have not
    fetched them, so charge a flat estimate rather than pretending they are free.
    """
    inline = sum(len(m.group(1)) for m in _SCRIPT_BLOCK.finditer(raw))
    external = len(_SCRIPT_SRC.findall(raw))
    return inline + external * 8 * 1024


def has_framework_marker(raw: bytes, cfg: RenderConfig) -> str | None:
    head = raw[:200_000]
    for marker in cfg.framework_markers:
        if marker.encode("utf-8", "ignore") in head:
            return marker
    return None


def in_audit_sample(url: str, cfg: RenderConfig, *, today: date | None = None) -> bool:
    """Deterministic per (URL, day) sampling.

    Deterministic so an audit result is reproducible while investigating; keyed
    on the day so coverage rotates rather than permanently excluding the same
    pages from ever being checked.
    """
    if cfg.audit_sample_rate <= 0:
        return False
    stamp = (today or date.today()).isoformat()
    digest = hashlib.blake2b(f"{url}|{stamp}".encode(), digest_size=8).digest()
    bucket = int.from_bytes(digest, "big") % 1_000_000
    return bucket < cfg.audit_sample_rate * 1_000_000


def decide(
    *,
    url: str,
    raw: bytes,
    static_text: str,
    host: str,
    cfg: RenderConfig,
    today: date | None = None,
) -> tuple[RenderDecision, str]:
    """Return the decision and a human-readable reason for logging."""
    if host in cfg.allow_list:
        return RenderDecision.RENDER_ALLOWLIST, f"host {host} on allow-list"

    if marker := has_framework_marker(raw, cfg):
        return RenderDecision.RENDER_FRAMEWORK, f"framework marker {marker!r}"

    text_len = len(static_text.strip())
    js = script_weight(raw)
    if text_len < cfg.min_static_text_chars and js > cfg.min_js_bytes:
        return RenderDecision.RENDER_THIN, f"{text_len} chars of text, ~{js // 1024} KB of JS"

    # The audit path. Without it there is no way to discover the rules above are
    # wrong, because a false negative produces no error signal at all.
    if in_audit_sample(url, cfg, today=today):
        return RenderDecision.RENDER_AUDIT, "sampled for classifier audit"

    return RenderDecision.STATIC, f"{text_len} chars of text, ~{js // 1024} KB of JS"
