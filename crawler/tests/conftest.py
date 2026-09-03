from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import fakeredis.aioredis  # noqa: E402

from atlas_crawler.config import Config, FetchConfig, PolitenessConfig  # noqa: E402
from atlas_crawler.dns import DNSCache  # noqa: E402


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=False)


@pytest.fixture
def cfg():
    return Config(
        user_agent="AtlasSearchTestBot/1.0 (+https://example.org/bot)",
        robots_agent="atlassearchtestbot",
        politeness=PolitenessConfig(initial_rate=2.0, burst=1, increase_after_ok=3),
        fetch=FetchConfig(max_body_bytes=4096, total_timeout=5.0, max_concurrent=8),
    )


class StubDNS(DNSCache):
    """Never touches the network; every host resolves to one IP unless told otherwise."""

    def __init__(self, mapping: dict[str, str] | None = None, default: str = "203.0.113.7"):
        super().__init__()
        self.mapping = mapping or {}
        self.default = default

    async def _lookup(self, host: str) -> tuple[str, ...]:
        ip = self.mapping.get(host, self.default)
        return (ip,) if ip else ()


@pytest.fixture
def dns():
    return StubDNS()
