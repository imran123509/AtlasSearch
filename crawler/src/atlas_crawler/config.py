"""Crawler configuration.

Defaults are deliberately conservative: this software makes requests to real
websites operated by real people. See `USER_AGENT` — it must carry a working
contact URL before this is pointed at anything outside your own hosts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class PolitenessConfig:
    # AIMD bounds. Start slow, earn throughput, back off hard.
    initial_rate: float = _env_f("CRAWL_INITIAL_RATE", 1.0)  # req/s per host
    min_rate: float = _env_f("CRAWL_MIN_RATE", 0.05)
    max_rate: float = _env_f("CRAWL_MAX_RATE", 5.0)
    burst: int = _env_i("CRAWL_BURST", 2)

    additive_increase: float = 0.25  # req/s added after a healthy streak
    multiplicative_decrease: float = 0.5  # rate *= this on error
    increase_after_ok: int = 10  # consecutive successes before increasing

    # A host whose latency rises gets relief before it starts erroring.
    latency_backoff_seconds: float = 5.0

    # Ceiling on how long a Retry-After we will honour before giving up on the
    # host for this cycle. Some servers send absurd values.
    max_retry_after_seconds: float = 300.0

    bucket_ttl_seconds: int = 3600


@dataclass(frozen=True)
class FetchConfig:
    # Hard per-request deadline. A tarpitting host must not hold a slot open.
    total_timeout: float = _env_f("CRAWL_TIMEOUT", 20.0)
    connect_timeout: float = 5.0
    read_timeout: float = 10.0

    max_body_bytes: int = _env_i("CRAWL_MAX_BODY", 10 * 1024 * 1024)
    max_redirects: int = 5
    max_concurrent: int = _env_i("CRAWL_CONCURRENCY", 32)

    # 5xx retry decay before the URL is given up on for this cycle.
    max_server_error_retries: int = 3

    accept: str = "text/html,application/xhtml+xml,application/pdf;q=0.8,*/*;q=0.5"
    accept_language: str = "en;q=0.9,*;q=0.5"


@dataclass(frozen=True)
class RobotsConfig:
    ttl_seconds: int = _env_i("ROBOTS_TTL", 4 * 3600)
    negative_ttl_seconds: int = 600  # cache "unreachable" briefly, then re-try
    max_bytes: int = 512 * 1024  # RFC 9309 says parse at least 500 KiB
    timeout: float = 10.0


@dataclass(frozen=True)
class DNSConfig:
    # getaddrinfo does not expose TTL, so we impose our own bounds. The Target
    # design runs recursive resolvers that honour real TTLs.
    ttl_seconds: int = 300
    ttl_floor_seconds: int = 60  # some sites publish 30s TTLs; do not thrash
    negative_ttl_seconds: int = 60
    timeout: float = 5.0


@dataclass(frozen=True)
class LeaseConfig:
    ttl_seconds: int = 300  # crash → lease expires → URL returns to the frontier


@dataclass(frozen=True)
class Config:
    user_agent: str = _env(
        "CRAWL_USER_AGENT",
        "AtlasSearchBot/0.1 (+https://example.org/bot; crawler@example.org)",
    )
    # The token the robots parser matches against. Must be the product token
    # from user_agent, lowercased, without version.
    robots_agent: str = _env("CRAWL_ROBOTS_AGENT", "atlassearchbot")

    redis_url: str = _env("REDIS_URL", "redis://localhost:6379/0")
    kafka_brokers: str = _env("KAFKA_BROKERS", "localhost:9092")
    s3_endpoint: str = _env("S3_ENDPOINT", "http://localhost:9000")
    s3_bucket: str = _env("S3_BUCKET", "atlas-raw")

    topic_fetched: str = "pages.fetched"
    topic_discovered: str = "urls.discovered"

    politeness: PolitenessConfig = field(default_factory=PolitenessConfig)
    fetch: FetchConfig = field(default_factory=FetchConfig)
    robots: RobotsConfig = field(default_factory=RobotsConfig)
    dns: DNSConfig = field(default_factory=DNSConfig)
    lease: LeaseConfig = field(default_factory=LeaseConfig)

    def validate(self) -> None:
        """Refuse to run with a user-agent that gives operators no recourse."""
        if "+http" not in self.user_agent:
            raise ValueError(
                "CRAWL_USER_AGENT must contain a contact URL (e.g. '+https://…/bot') "
                "so site operators can identify and block this crawler."
            )
