"""Prometheus metrics for the parse stage.

The corpus-health gauges here are the ones features/MONITORING.md singles out as
having no other error signal. `parse_extracted_text_length` in particular: if the
boilerplate remover starts eating main content after a deploy, documents index
fine, queries succeed, latency is normal, and result quality quietly collapses.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

PARSED = Counter("parse_documents_total", "Documents parsed", ["outcome"])
REJECTED = Counter("parse_rejected_total", "Documents refused before indexing", ["reason"])
DURATION = Histogram(
    "parse_duration_seconds", "Parse wall time",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 5),
)

# Corpus health — watch the distribution, not any single document.
EXTRACTED_LENGTH = Histogram(
    "parse_extracted_text_length", "Main-content characters kept per document",
    buckets=(0, 100, 250, 500, 1000, 2500, 5000, 10_000, 25_000, 100_000),
)
RETAINED_RATIO = Histogram(
    "parse_retained_ratio", "Kept chars / visible chars",
    buckets=(0.01, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 1.0),
)
WARNINGS = Counter("parse_warnings_total", "Parse warnings by kind", ["warning"])

RENDER_DECISIONS = Counter(
    "parse_render_decisions_total", "Render classifier decisions", ["decision"]
)
# Rising = sites silently missing from the index. The audit path exists to
# populate this; without it the number cannot be known.
RENDER_AUDIT_DISAGREE = Counter(
    "parse_render_audit_disagreements_total",
    "Audit renders that found substantially more content than the static parse",
)

CHARSET_SOURCE = Counter("parse_charset_source_total", "How encoding was resolved", ["source"])
LANGUAGES = Counter("parse_language_total", "Detected language", ["lang"])
LANG_DISAGREE = Counter(
    "parse_language_disagreement_total", "Model disagreed with the lang attribute"
)

LINKS_EXTRACTED = Counter("parse_links_extracted_total", "Outbound links", ["rel"])
LINKS_PER_DOC = Histogram(
    "parse_links_per_document", "Outbound links per document",
    buckets=(0, 5, 15, 40, 100, 250, 600, 1500, 3000),
)

TEMPLATE_HOSTS = Gauge("parse_template_hosts", "Hosts with enough pages for template learning")


def observe(doc) -> None:  # noqa: ANN001 - avoids a circular import on ParsedDocument
    """Record one successfully parsed document."""
    DURATION.observe(doc.parse_ms / 1000.0)
    EXTRACTED_LENGTH.observe(len(doc.body))
    if doc.visible_chars:
        RETAINED_RATIO.observe(doc.retained_ratio)
    RENDER_DECISIONS.labels(str(doc.render)).inc()
    if doc.charset:
        CHARSET_SOURCE.labels(doc.charset.source).inc()
    if doc.language:
        LANGUAGES.labels(doc.language.code).inc()
        if doc.language.disagreed:
            LANG_DISAGREE.inc()
    LINKS_PER_DOC.observe(len(doc.links))
    for link in doc.links:
        LINKS_EXTRACTED.labels(str(link.rel)).inc()
    for warning in doc.warnings:
        WARNINGS.labels(warning.split(":")[0]).inc()
    PARSED.labels("indexable" if doc.is_indexable else "not_indexable").inc()
