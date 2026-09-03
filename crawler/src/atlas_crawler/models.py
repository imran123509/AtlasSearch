"""Data types passed between crawler stages."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Outcome(enum.StrEnum):
    """Terminal classification of a fetch attempt.

    Maps to the response-handling table in features/WEB-CRAWLER.md.
    """

    FETCHED = "fetched"  # 200, body stored
    NOT_MODIFIED = "not_modified"  # 304, metadata touched only
    REDIRECTED = "redirected"  # 3xx, target requeued
    GONE = "gone"  # 404/410, tombstone
    CLIENT_ERROR = "client_error"  # other 4xx
    SERVER_ERROR = "server_error"  # 5xx, retry with decay
    THROTTLED = "throttled"  # 429/503, whole host backed off
    ROBOTS_DENIED = "robots_denied"
    ROBOTS_UNREACHABLE = "robots_unreachable"  # fail closed
    RATE_LIMITED = "rate_limited"  # our own limiter said not yet
    DNS_FAILED = "dns_failed"
    TIMEOUT = "timeout"
    TOO_LARGE = "too_large"
    REDIRECT_LOOP = "redirect_loop"
    NETWORK_ERROR = "network_error"

    @property
    def counts_against_host_health(self) -> bool:
        """Whether this outcome should drive the AIMD limiter downward.

        Our own limiter declining a fetch is not the host's fault, and robots
        denial is a successful interaction — neither should reduce the rate.
        """
        return self in {
            Outcome.SERVER_ERROR,
            Outcome.THROTTLED,
            Outcome.TIMEOUT,
            Outcome.NETWORK_ERROR,
        }

    @property
    def is_success(self) -> bool:
        return self in {Outcome.FETCHED, Outcome.NOT_MODIFIED, Outcome.REDIRECTED}


@dataclass(slots=True)
class CrawlTask:
    """One URL leased from the frontier."""

    url: str
    depth: int = 0
    priority: int = 500
    source_url: str | None = None
    # Conditional-GET validators from the previous fetch, if any.
    etag: str | None = None
    last_modified: str | None = None
    # Incremented across 5xx retries; the fetcher gives up past the configured max.
    attempt: int = 0
    lease_token: str | None = None


@dataclass(slots=True)
class HostKey:
    """Politeness is keyed on BOTH of these.

    Hostname alone is wrong: one shared-hosting IP serves thousands of vhosts,
    and crawling them "politely" in parallel still melts one machine.
    IP alone is wrong: a CDN-fronted site presents a few IPs for millions of
    pages we are entitled to crawl faster.
    """

    domain: str  # registrable domain, e.g. "example.co.uk"
    ip: str

    def as_tuple(self) -> tuple[str, str]:
        return (self.domain, self.ip)


@dataclass(slots=True)
class FetchResult:
    url: str
    outcome: Outcome
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    content_hash: str | None = None
    elapsed_s: float = 0.0
    fetched_at: datetime = field(default_factory=utcnow)

    # Populated for REDIRECTED
    redirect_target: str | None = None
    redirect_permanent: bool = False

    # Populated when the host asked us to back off
    retry_after_s: float | None = None

    error: str | None = None

    @property
    def content_type(self) -> str | None:
        return self.headers.get("content-type")

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")


@dataclass(slots=True)
class BlobRef:
    """Where the body landed. This — not the body — goes on the Kafka topic."""

    key: str
    size_bytes: int
    content_hash: str
