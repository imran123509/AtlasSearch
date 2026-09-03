"""Politeness enforcement: paired token buckets + AIMD rate control.

Two properties this module exists to guarantee:

1. **Atomicity.** The read-modify-write in a token bucket is a classic race —
   two fetchers reading `tokens=1` simultaneously both proceed and politeness is
   broken. Every decision happens inside one Lua script, which Redis runs
   atomically.

2. **Both keys.** A fetch needs budget on the registrable domain *and* on the
   resolved IP. They are checked in a single script so we can never deduct from
   one and be denied by the other (which would leak tokens and slowly starve
   well-behaved hosts).

The Target design moves this in-process — a Redis round trip per fetch decision
does not survive 10K fetches/s, and Kafka domain-keyed partitioning already gives
each worker exclusive ownership of its hosts. See features/DISTRIBUTED-CRAWLER.md.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import structlog

from .config import PolitenessConfig
from .models import HostKey

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Lua: paired token bucket with an explicit pause gate.
#
# KEYS[1] domain bucket hash   KEYS[2] ip bucket hash   KEYS[3] pause key
# ARGV    1 now_ms  2 d_rate  3 d_burst  4 i_rate  5 i_burst  6 cost  7 ttl_s
# returns {allowed, retry_after_ms, reason}
# ---------------------------------------------------------------------------
_ACQUIRE_LUA = """
local now    = tonumber(ARGV[1])
local cost   = tonumber(ARGV[6])
local ttl    = tonumber(ARGV[7])

-- A host we were told to back off from is closed regardless of token state.
local pause = redis.call('PTTL', KEYS[3])
if pause > 0 then
  return {0, pause, 'paused'}
end

local function peek(key, rate, burst)
  local h  = redis.call('HMGET', key, 'tokens', 'ts')
  local tk = tonumber(h[1])
  local ts = tonumber(h[2])
  if tk == nil then tk = burst end
  if ts == nil then ts = now end
  tk = math.min(burst, tk + (now - ts) / 1000.0 * rate)
  return tk
end

local d_rate, d_burst = tonumber(ARGV[2]), tonumber(ARGV[3])
local i_rate, i_burst = tonumber(ARGV[4]), tonumber(ARGV[5])

local d_tokens = peek(KEYS[1], d_rate, d_burst)
local i_tokens = peek(KEYS[2], i_rate, i_burst)

-- Deny before deducting anything, so the two buckets can never drift apart.
if d_tokens < cost then
  return {0, math.ceil((cost - d_tokens) / d_rate * 1000), 'domain'}
end
if i_tokens < cost then
  return {0, math.ceil((cost - i_tokens) / i_rate * 1000), 'ip'}
end

redis.call('HMSET', KEYS[1], 'tokens', d_tokens - cost, 'ts', now)
redis.call('EXPIRE', KEYS[1], ttl)
redis.call('HMSET', KEYS[2], 'tokens', i_tokens - cost, 'ts', now)
redis.call('EXPIRE', KEYS[2], ttl)
return {1, 0, 'ok'}
"""


# ---------------------------------------------------------------------------
# Lua: AIMD update. Additive increase after a healthy streak, multiplicative
# decrease on any error or on rising latency.
#
# KEYS[1] host state hash
# ARGV 1 signal(ok|slow|err) 2 ai 3 md 4 min 5 max 6 initial 7 streak 8 ttl_s
# returns new rate (string)
# ---------------------------------------------------------------------------
_AIMD_LUA = """
local signal  = ARGV[1]
local ai      = tonumber(ARGV[2])
local md      = tonumber(ARGV[3])
local rmin    = tonumber(ARGV[4])
local rmax    = tonumber(ARGV[5])
local initial = tonumber(ARGV[6])
local streak  = tonumber(ARGV[7])
local ttl     = tonumber(ARGV[8])

local h    = redis.call('HMGET', KEYS[1], 'rate', 'ok')
local rate = tonumber(h[1]) or initial
local ok   = tonumber(h[2]) or 0

if signal == 'err' then
  rate = math.max(rmin, rate * md)
  ok = 0
elseif signal == 'slow' then
  -- Back off before the host starts erroring, but less sharply.
  rate = math.max(rmin, rate * (1.0 - (1.0 - md) / 2.0))
  ok = 0
else
  ok = ok + 1
  if ok >= streak then
    rate = math.min(rmax, rate + ai)
    ok = 0
  end
end

redis.call('HMSET', KEYS[1], 'rate', rate, 'ok', ok)
redis.call('EXPIRE', KEYS[1], ttl)
return tostring(rate)
"""


@dataclass(slots=True)
class Decision:
    allowed: bool
    retry_after_s: float = 0.0
    reason: str = "ok"


class PolitenessLimiter:
    def __init__(self, redis: Any, config: PolitenessConfig | None = None) -> None:
        self.redis = redis
        self.cfg = config or PolitenessConfig()
        self._acquire = redis.register_script(_ACQUIRE_LUA)
        self._aimd = redis.register_script(_AIMD_LUA)

    # -- keys ---------------------------------------------------------------
    # NOTE: cluster deployments need hash tags so a domain's keys share a slot.
    @staticmethod
    def _domain_key(domain: str) -> str:
        return f"rl:host:{domain}"

    @staticmethod
    def _ip_key(ip: str) -> str:
        return f"rl:ip:{ip}"

    @staticmethod
    def _pause_key(domain: str) -> str:
        return f"pause:{domain}"

    @staticmethod
    def _state_key(domain: str) -> str:
        return f"hs:{domain}"

    # -- api ----------------------------------------------------------------
    async def acquire(self, key: HostKey, *, cost: int = 1) -> Decision:
        """Try to spend one fetch's worth of budget for this (domain, ip)."""
        rate = await self.current_rate(key.domain)
        res = await self._acquire(
            keys=[
                self._domain_key(key.domain),
                self._ip_key(key.ip),
                self._pause_key(key.domain),
            ],
            args=[
                int(time.time() * 1000),
                rate,
                self.cfg.burst,
                # The IP bucket is deliberately more generous than the domain
                # bucket: it is a backstop against shared hosting, not the
                # primary control.
                rate * 3,
                self.cfg.burst * 3,
                cost,
                self.cfg.bucket_ttl_seconds,
            ],
        )
        allowed = bool(int(res[0]))
        retry_ms = int(res[1])
        reason = res[2].decode() if isinstance(res[2], bytes) else str(res[2])
        return Decision(allowed=allowed, retry_after_s=retry_ms / 1000.0, reason=reason)

    async def current_rate(self, domain: str) -> float:
        raw = await self.redis.hget(self._state_key(domain), "rate")
        if raw is None:
            return self.cfg.initial_rate
        try:
            return float(raw)
        except (TypeError, ValueError):
            return self.cfg.initial_rate

    async def apply_rate_ceiling(self, domain: str, rate: float) -> bool:
        """Clamp a host's rate down to `rate` if we are currently faster.

        Used for a declared `Crawl-delay`, which is a floor on our interval —
        never a licence to go faster than AIMD has earned.
        """
        rate = max(rate, self.cfg.min_rate)
        if rate >= await self.current_rate(domain):
            return False
        await self.redis.hset(self._state_key(domain), "rate", rate)
        await self.redis.expire(self._state_key(domain), self.cfg.bucket_ttl_seconds)
        return True

    async def record(self, domain: str, *, signal: str) -> float:
        """Feed the AIMD controller. `signal` is one of ok | slow | err."""
        new_rate = await self._aimd(
            keys=[self._state_key(domain)],
            args=[
                signal,
                self.cfg.additive_increase,
                self.cfg.multiplicative_decrease,
                self.cfg.min_rate,
                self.cfg.max_rate,
                self.cfg.initial_rate,
                self.cfg.increase_after_ok,
                self.cfg.bucket_ttl_seconds,
            ],
        )
        return float(new_rate)

    async def record_outcome(self, domain: str, *, ok: bool, elapsed_s: float) -> float:
        if not ok:
            return await self.record(domain, signal="err")
        if elapsed_s > self.cfg.latency_backoff_seconds:
            return await self.record(domain, signal="slow")
        return await self.record(domain, signal="ok")

    async def pause_host(self, domain: str, seconds: float, *, reason: str = "") -> None:
        """Back off the whole host — not just this URL.

        A 429 or 503 is a statement about the server, so every URL on it waits.
        """
        seconds = max(1.0, min(seconds, self.cfg.max_retry_after_seconds))
        await self.redis.set(self._pause_key(domain), reason or "1", ex=int(seconds))
        await self.record(domain, signal="err")
        log.info("politeness.host_paused", domain=domain, seconds=seconds, reason=reason)

    async def paused_for(self, domain: str) -> float:
        ttl = await self.redis.pttl(self._pause_key(domain))
        return max(0.0, ttl / 1000.0) if ttl and ttl > 0 else 0.0
