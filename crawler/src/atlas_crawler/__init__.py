"""AtlasSearch crawler — fetch loop, politeness, robots, DNS, frontier.

Implements features/WEB-CRAWLER.md (Build layer).

The one thing to know before changing anything here: politeness is enforced in
exactly one place (`ratelimit.PolitenessLimiter`, reached only via
`fetcher.Fetcher._gate`) and it cannot be bypassed by a caller. Keep it that way.

Top-level names are resolved lazily (PEP 562) so that a consumer needing only
`atlas_crawler.urlnorm` — the indexer shares it, because divergent
canonicalisation would give one URL two fingerprints — does not pull in httpx,
redis, and the rest of the fetch stack.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config
    from .fetcher import Fetcher
    from .frontier import Frontier
    from .models import CrawlTask, FetchResult, HostKey, Outcome
    from .ratelimit import PolitenessLimiter
    from .robots import RobotsCache

_LAZY = {
    "Config": ".config",
    "CrawlTask": ".models",
    "FetchResult": ".models",
    "HostKey": ".models",
    "Outcome": ".models",
    "Fetcher": ".fetcher",
    "Frontier": ".frontier",
    "PolitenessLimiter": ".ratelimit",
    "RobotsCache": ".robots",
}

__all__ = [*_LAZY]
__version__ = "0.1.0"


def __getattr__(name: str):
    """Import submodules on first attribute access, not at package import."""
    if name in _LAZY:
        import importlib

        module = importlib.import_module(_LAZY[name], __name__)
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), *_LAZY])
