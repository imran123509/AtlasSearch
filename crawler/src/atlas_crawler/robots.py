"""robots.txt fetching, caching, and evaluation.

**Fail closed.** An unfetchable robots.txt means *do not crawl*, not *crawl
freely*. This is the single most common correctness bug in hobby crawlers, and
it is the one with real-world consequences for the sites being crawled.

Status handling follows RFC 9309 §2.3.1 with one deliberate deviation, noted below.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import structlog
from protego import Protego

from .config import RobotsConfig

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class RobotsRules:
    """Parsed rules for one origin, plus why we have them."""

    origin: str
    reachable: bool
    body: str | None
    fetched_at: float

    _parsed: Protego | None = None

    def __post_init__(self) -> None:
        if self.body is not None and self._parsed is None:
            try:
                self._parsed = Protego.parse(self.body)
            except Exception as exc:  # noqa: BLE001 - a malformed file must not crash the crawl
                log.warning("robots.parse_failed", origin=self.origin, error=str(exc))
                self._parsed = None

    def can_fetch(self, url: str, agent: str) -> bool:
        if not self.reachable:
            return False  # fail closed
        if self._parsed is None:
            # Reachable but unparseable: treat as permissive, matching RFC 9309's
            # "parse what you can" guidance. An empty/garbage file is not a block.
            return True
        try:
            return bool(self._parsed.can_fetch(url, agent))
        except Exception:  # noqa: BLE001
            return False

    def crawl_delay(self, agent: str) -> float | None:
        """Longest delay declared for us or for `*`.

        Robots semantics say the most specific matching group applies
        *exclusively* — so a site with `Crawl-delay: 5` under `User-agent: *`
        and a separate group naming our bot would, read strictly, impose no
        delay on us at all. That is the aggressive reading of an operator who
        was plainly asking for slower crawling. We take the maximum of both.
        """
        if self._parsed is None:
            return None
        delays: list[float] = []
        for who in (agent, "*"):
            try:
                d = self._parsed.crawl_delay(who)
            except Exception:  # noqa: BLE001
                continue
            if d is not None:
                delays.append(float(d))
        return max(delays) if delays else None

    @property
    def sitemaps(self) -> list[str]:
        if self._parsed is None:
            return []
        try:
            return list(self._parsed.sitemaps)
        except Exception:  # noqa: BLE001
            return []


def origin_of(url: str) -> str:
    """robots.txt is scoped to scheme+host+port, not to the registrable domain."""
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "", "", ""))


class RobotsCache:
    def __init__(
        self,
        client: httpx.AsyncClient,
        redis: Any,
        *,
        user_agent: str,
        robots_agent: str,
        config: RobotsConfig | None = None,
    ) -> None:
        self.client = client
        self.redis = redis
        self.user_agent = user_agent
        self.robots_agent = robots_agent
        self.cfg = config or RobotsConfig()
        self._local: dict[str, RobotsRules] = {}

    @staticmethod
    def _key(origin: str) -> str:
        return f"robots:{origin}"

    async def get(self, url: str) -> RobotsRules:
        origin = origin_of(url)

        cached = self._local.get(origin)
        if cached is not None and not self._stale(cached):
            return cached

        raw = await self.redis.get(self._key(origin))
        if raw is not None:
            body = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            reachable = not body.startswith("\x00UNREACHABLE")
            rules = RobotsRules(
                origin=origin,
                reachable=reachable,
                body=body if reachable else None,
                fetched_at=time.monotonic(),
            )
            self._local[origin] = rules
            return rules

        rules = await self._fetch(origin)
        self._local[origin] = rules
        ttl = self.cfg.ttl_seconds if rules.reachable else self.cfg.negative_ttl_seconds
        await self.redis.set(
            self._key(origin),
            rules.body if rules.reachable else "\x00UNREACHABLE",
            ex=ttl,
        )
        return rules

    def _stale(self, rules: RobotsRules) -> bool:
        ttl = self.cfg.ttl_seconds if rules.reachable else self.cfg.negative_ttl_seconds
        return (time.monotonic() - rules.fetched_at) > ttl

    async def _fetch(self, origin: str) -> RobotsRules:
        url = f"{origin}/robots.txt"
        now = time.monotonic()
        try:
            resp = await self.client.get(
                url,
                headers={"User-Agent": self.user_agent, "Accept": "text/plain,*/*;q=0.5"},
                timeout=self.cfg.timeout,
                follow_redirects=True,  # RFC 9309: follow up to 5; httpx caps this
            )
        except httpx.HTTPError as exc:
            log.info("robots.unreachable", origin=origin, error=type(exc).__name__)
            return RobotsRules(origin, reachable=False, body=None, fetched_at=now)

        code = resp.status_code

        if 200 <= code < 300:
            body = resp.content[: self.cfg.max_bytes].decode("utf-8", "replace")
            return RobotsRules(origin, reachable=True, body=body, fetched_at=now)

        if code in (404, 410):
            # RFC 9309: "unavailable" — no rules exist, so everything is allowed.
            return RobotsRules(origin, reachable=True, body="", fetched_at=now)

        if code in (401, 403):
            # DEVIATION from RFC 9309, which classes these as "unavailable"
            # (allow all). A server demanding credentials for robots.txt is
            # signalling deliberate access control; crawling it anyway is not
            # defensible. We fail closed.
            log.info("robots.access_controlled", origin=origin, status=code)
            return RobotsRules(origin, reachable=False, body=None, fetched_at=now)

        # 429 and 5xx: "unreachable" — assume complete disallow.
        log.info("robots.unreachable", origin=origin, status=code)
        return RobotsRules(origin, reachable=False, body=None, fetched_at=now)

    async def can_fetch(self, url: str) -> tuple[bool, RobotsRules]:
        rules = await self.get(url)
        return rules.can_fetch(url, self.robots_agent), rules


def header_forbids_indexing(headers: dict[str, str], agent_token: str) -> bool:
    """Honour `X-Robots-Tag: noindex` (and `none`) from the response headers.

    Directives may be agent-scoped: `X-Robots-Tag: atlassearchbot: noindex`.
    """
    raw = headers.get("x-robots-tag")
    if not raw:
        return False
    for directive in raw.split(","):
        directive = directive.strip().lower()
        if ":" in directive:
            who, _, what = directive.partition(":")
            if who.strip() not in (agent_token, "*"):
                continue
            directive = what.strip()
        if directive in ("noindex", "none"):
            return True
    return False
