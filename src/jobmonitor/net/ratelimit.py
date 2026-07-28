"""Per-host token bucket.

Rate limiting is keyed by host rather than by company because several
companies share one ATS host (``*.myworkdayjobs.com``, ``app.eightfold.ai``).
Limiting per company would let twenty companies hammer one host in parallel.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict


class TokenBucket:
    """Classic token bucket: `rate` tokens/sec, capped at `burst`."""

    __slots__ = ("rate", "burst", "_tokens", "_updated", "_lock")

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        # The lock serialises the read-modify-write on the token count, so
        # concurrent callers cannot both see the same available token.
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._updated
                self._updated = now
                self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = (1.0 - self._tokens) / self.rate
                await asyncio.sleep(deficit)


class HostRateLimiter:
    """Lazily creates one bucket per host."""

    def __init__(self, rate: float, burst: int) -> None:
        self._rate = rate
        self._burst = burst
        self._buckets: Dict[str, TokenBucket] = {}
        self._lock = asyncio.Lock()
        # Hosts that returned 429; we back off harder until the deadline.
        self._penalties: Dict[str, float] = {}

    async def acquire(self, host: str) -> None:
        async with self._lock:
            bucket = self._buckets.get(host)
            if bucket is None:
                bucket = TokenBucket(self._rate, self._burst)
                self._buckets[host] = bucket
            penalty_until = self._penalties.get(host, 0.0)

        now = time.monotonic()
        if penalty_until > now:
            await asyncio.sleep(penalty_until - now)
        await bucket.acquire()

    def penalise(self, host: str, seconds: float) -> None:
        """Called on 429/503 so the next requests to that host wait it out."""
        deadline = time.monotonic() + max(0.0, seconds)
        current = self._penalties.get(host, 0.0)
        self._penalties[host] = max(current, deadline)

    def stats(self) -> Dict[str, float]:
        now = time.monotonic()
        return {h: max(0.0, until - now) for h, until in self._penalties.items() if until > now}
