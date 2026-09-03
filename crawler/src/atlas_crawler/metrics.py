"""Prometheus metrics. Names match features/MONITORING.md."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

FETCHES = Counter(
    "crawl_fetches_total", "Fetch attempts by outcome and status class", ["outcome", "status_class"]
)
FETCH_DURATION = Histogram(
    "crawl_fetch_duration_seconds",
    "Wall time per fetch",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 60),
)
ROBOTS_DENIED = Counter("crawl_robots_denied_total", "Fetches declined by robots.txt", ["reason"])

# Must be zero. Alert on any occurrence — this is legal and ethical exposure,
# not a performance metric, so it has no acceptable threshold.
POLITENESS_VIOLATIONS = Counter(
    "crawl_politeness_violations_total",
    "Concurrent in-flight requests sharing a politeness key",
    ["kind"],
)

BYTES_FETCHED = Counter("crawl_bytes_fetched_total", "Body bytes downloaded")
HOSTS_SCHEDULED = Gauge("crawl_frontier_hosts_scheduled", "Hosts on the due heap")
HOSTS_DUE = Gauge("crawl_frontier_hosts_due", "Hosts due for fetch now")
IDLE_FETCHERS = Gauge("crawl_frontier_idle_fetchers", "Fetch slots idle while work exists")
HOST_RATE = Gauge("crawl_host_rate_limit", "Current AIMD rate for a host", ["domain"])
DNS_CACHE = Gauge("crawl_dns_cache_entries", "DNS cache entries", ["state"])


def status_class(status: int | None) -> str:
    if status is None:
        return "none"
    return f"{status // 100}xx"
