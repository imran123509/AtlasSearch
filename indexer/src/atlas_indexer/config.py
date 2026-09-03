"""Parser configuration and safety limits.

Every limit here exists because some page on the open web will hit it. Real HTML
is broken, hostile, or enormous often enough that the parser must be bounded on
every axis before it sees a byte.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class ParseLimits:
    max_bytes: int = _i("PARSE_MAX_BYTES", 10 * 1024 * 1024)
    max_nodes: int = _i("PARSE_MAX_NODES", 500_000)
    max_depth: int = _i("PARSE_MAX_DEPTH", 200)
    timeout_seconds: float = _f("PARSE_TIMEOUT", 10.0)

    # A page with 50,000 links is a link farm or a bug, not a document.
    max_links: int = _i("PARSE_MAX_LINKS", 3_000)
    max_anchor_chars: int = 300
    anchor_context_chars: int = 40  # "click here" is useless; the sentence is not


@dataclass(frozen=True)
class BoilerplateConfig:
    # Kohlschütter et al. shallow-text thresholds. See boilerplate.py.
    link_density_max: float = 0.333
    prev_link_density_max: float = 0.555

    min_block_words: int = 3

    # Site-template subtraction: a block whose exact text appears on this
    # fraction of a host's sampled pages is chrome, not content.
    template_min_pages: int = 4
    template_repeat_ratio: float = 0.6

    # Guard from the doc's failure table: if extraction keeps a suspiciously
    # small share of visible text, the remover probably ate the article.
    min_retained_ratio: float = 0.04
    min_retained_chars: int = 140


@dataclass(frozen=True)
class RenderConfig:
    """The render decision is a quality lever hiding inside a cost knob."""

    min_static_text_chars: int = 200
    min_js_bytes: int = 20 * 1024

    # NOT optional. Without a sampled audit path there is no way to discover the
    # classifier is wrong — a false negative produces no error, just a site
    # silently missing from the index.
    audit_sample_rate: float = _f("RENDER_AUDIT_RATE", 0.01)

    allow_list: frozenset[str] = frozenset()

    framework_markers: tuple[str, ...] = (
        "__NEXT_DATA__", "__NUXT__", "__remixContext", "window.__INITIAL_STATE__",
        "ng-app", "ng-version", "data-reactroot", "data-react-helmet",
        "id=\"root\"", "id=\"app\"", "id=\"__next\"", "v-cloak", "data-svelte",
        "astro-island", "data-server-rendered",
    )


@dataclass(frozen=True)
class LanguageConfig:
    min_chars: int = 40  # below this, identification is noise
    min_confidence: float = 0.55
    # The `lang` attribute is a prior, never truth — it is wrong often enough
    # to matter, and a wrong language corrupts tokenisation permanently.
    attribute_prior_weight: float = 0.25


@dataclass(frozen=True)
class Config:
    kafka_brokers: str = os.environ.get("KAFKA_BROKERS", "localhost:9092")
    s3_endpoint: str = os.environ.get("S3_ENDPOINT", "http://localhost:9000")
    s3_bucket: str = os.environ.get("S3_BUCKET", "atlas-raw")

    topic_in: str = "pages.fetched"
    topic_out: str = "pages.parsed"
    topic_links: str = "links.extracted"
    topic_discovered: str = "urls.discovered"
    consumer_group: str = "indexer-parse"

    limits: ParseLimits = field(default_factory=ParseLimits)
    boilerplate: BoilerplateConfig = field(default_factory=BoilerplateConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    language: LanguageConfig = field(default_factory=LanguageConfig)

    # Field weights are consumed by BM25F at query time; they live here so the
    # indexer and the ranker cannot disagree about what a field is worth.
    field_weights: dict[str, float] = field(
        default_factory=lambda: {
            "anchors": 10.0,   # filled in later, by DISTRIBUTED-INDEXING's shuffle
            "title": 8.0,
            "headings": 3.0,
            "url_text": 2.5,
            "body": 1.0,
            "meta_description": 0.5,  # frequently spam
        }
    )
