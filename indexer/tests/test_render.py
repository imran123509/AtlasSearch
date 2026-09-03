from __future__ import annotations

from datetime import date

import pytest

from atlas_indexer.config import RenderConfig
from atlas_indexer.models import RenderDecision
from atlas_indexer.render import decide, has_framework_marker, in_audit_sample, script_weight

NO_AUDIT = RenderConfig(audit_sample_rate=0.0)


def _decide(**kw):
    base = dict(url="https://example.com/p", raw=b"<html><body>x</body></html>",
                static_text="", host="example.com", cfg=NO_AUDIT)
    return decide(**{**base, **kw})


class TestDecision:
    def test_thin_page_with_heavy_js_is_rendered(self):
        raw = b"<html><body><div id=x></div><script>" + b"j" * 30_000 + b"</script></body></html>"
        decision, _ = _decide(raw=raw, static_text="tiny")
        assert decision is RenderDecision.RENDER_THIN

    def test_thin_page_without_js_is_not_rendered(self):
        decision, _ = _decide(raw=b"<html><body>short</body></html>", static_text="short")
        assert decision is RenderDecision.STATIC

    def test_heavy_js_with_plenty_of_text_is_not_rendered(self):
        """A normal article that happens to ship analytics does not need a browser."""
        raw = b"<html><body><script>" + b"j" * 40_000 + b"</script></body></html>"
        decision, _ = _decide(raw=raw, static_text="a" * 5_000)
        assert decision is RenderDecision.STATIC

    @pytest.mark.parametrize(
        "marker", [b"__NEXT_DATA__", b"__NUXT__", b"ng-version", b"data-reactroot", b"astro-island"]
    )
    def test_framework_markers_force_a_render(self, marker):
        raw = b"<html><body><div " + marker + b"></div>" + b"a" * 5_000 + b"</body></html>"
        decision, reason = _decide(raw=raw, static_text="a" * 5_000)
        assert decision is RenderDecision.RENDER_FRAMEWORK
        assert marker.decode() in reason

    def test_allow_list_forces_a_render(self):
        cfg = RenderConfig(audit_sample_rate=0.0, allow_list=frozenset({"spa.test"}))
        decision, _ = _decide(host="spa.test", static_text="a" * 5_000, cfg=cfg)
        assert decision is RenderDecision.RENDER_ALLOWLIST

    def test_allow_list_beats_everything(self):
        cfg = RenderConfig(audit_sample_rate=0.0, allow_list=frozenset({"spa.test"}))
        decision, _ = _decide(host="spa.test", raw=b"__NEXT_DATA__", cfg=cfg)
        assert decision is RenderDecision.RENDER_ALLOWLIST

    def test_reason_is_always_populated_for_logging(self):
        _, reason = _decide(static_text="a" * 5_000)
        assert reason


class TestAuditPath:
    """The audit sample is not optional.

    A false negative from the classifier produces no error and no alert — just a
    site silently missing from the index. Sampling is the only way to find out.
    """

    def test_full_sampling_audits_everything(self):
        cfg = RenderConfig(audit_sample_rate=1.0)
        decision, reason = _decide(static_text="a" * 5_000, cfg=cfg)
        assert decision is RenderDecision.RENDER_AUDIT
        assert "audit" in reason

    def test_zero_rate_disables_it(self):
        assert not in_audit_sample("https://example.com/p", RenderConfig(audit_sample_rate=0.0))

    def test_sampling_is_deterministic_within_a_day(self):
        """Reproducible while investigating an audit result."""
        cfg = RenderConfig(audit_sample_rate=0.5)
        day = date(2026, 9, 3)
        first = in_audit_sample("https://example.com/p", cfg, today=day)
        assert all(in_audit_sample("https://example.com/p", cfg, today=day) == first for _ in range(20))

    def test_sampling_rotates_across_days(self):
        """Keyed on the day so coverage rotates, rather than permanently
        excluding the same pages from ever being checked."""
        cfg = RenderConfig(audit_sample_rate=0.5)
        url = "https://example.com/never-checked"
        seen = {in_audit_sample(url, cfg, today=date(2026, 9, d)) for d in range(1, 29)}
        assert seen == {True, False}, "the same URL is always or never audited"

    def test_rate_is_approximately_honoured(self):
        cfg = RenderConfig(audit_sample_rate=0.10)
        day = date(2026, 9, 3)
        hits = sum(in_audit_sample(f"https://example.com/p{i}", cfg, today=day) for i in range(4000))
        assert 0.07 < hits / 4000 < 0.13

    def test_audit_does_not_override_a_real_render_reason(self):
        cfg = RenderConfig(audit_sample_rate=1.0)
        decision, _ = _decide(raw=b"<div __NUXT__></div>", static_text="a" * 5_000, cfg=cfg)
        assert decision is RenderDecision.RENDER_FRAMEWORK


class TestScriptWeight:
    def test_inline_script_bytes_counted(self):
        assert script_weight(b"<script>" + b"x" * 1000 + b"</script>") >= 1000

    def test_external_scripts_charged_a_nominal_cost(self):
        """We have not fetched them, but they are usually what builds the page."""
        assert script_weight(b'<script src="/a.js"></script><script src="/b.js"></script>') > 0

    def test_no_scripts_is_zero(self):
        assert script_weight(b"<html><body><p>text</p></body></html>") == 0


class TestFrameworkMarkers:
    def test_found(self):
        assert has_framework_marker(b'<div id="__next"></div>', RenderConfig()) is not None

    def test_absent(self):
        assert has_framework_marker(b"<html><body><p>plain</p></body></html>", RenderConfig()) is None

    def test_only_the_head_of_the_document_is_scanned(self):
        raw = b"x" * 300_000 + b"__NUXT__"
        assert has_framework_marker(raw, RenderConfig()) is None


class TestNeedsBrowser:
    def test_static_does_not(self):
        assert RenderDecision.STATIC.needs_browser is False

    @pytest.mark.parametrize("d", [
        RenderDecision.RENDER_THIN, RenderDecision.RENDER_FRAMEWORK,
        RenderDecision.RENDER_ALLOWLIST, RenderDecision.RENDER_AUDIT,
    ])
    def test_render_decisions_do(self, d):
        assert d.needs_browser is True
