"""Per-host token buckets.

Per-host and not global, because the configured limits differ by more than an
order of magnitude across the APIs this package touches: NCBI E-utilities is
3/s unkeyed and 10/s keyed, Semantic Scholar is 1/s unkeyed, Crossref's polite
pool is 10/s, and arXiv now wants roughly one request every three seconds. A
single global limiter set safe enough for arXiv would make a PMC-heavy batch
take all day; set fast enough for Crossref it would get us 429'd off arXiv.

Buckets are process-wide and lock-guarded so the ThreadPoolExecutor in
batch_fetch_pdfs shares one budget per host rather than one per worker.
"""

import os
import threading
import time
from typing import Dict, Optional


class TokenBucket:
    """Classic token bucket: `rate` tokens/second, capped at `burst`."""

    def __init__(self, rate: float, burst: float):
        self.rate = max(float(rate), 0.001)
        self.burst = max(float(burst), 1.0)
        self._tokens = self.burst
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> float:
        """Block until `tokens` are available. Returns seconds actually slept."""
        slept = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.burst, self._tokens + (now - self._last) * self.rate
                )
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return slept
                deficit = tokens - self._tokens
                wait = deficit / self.rate
            time.sleep(wait)
            slept += wait

    def penalize(self, seconds: float) -> None:
        """Drain the bucket after a 429, so the next caller waits it out."""
        with self._lock:
            self._tokens = 0.0
            self._last = time.monotonic() + max(0.0, seconds)


#: The process-wide limiter. Politeness is a property of the PROCESS, not of a
#: batch: an API's allowance does not multiply because we started a second
#: walk. Three call sites used to build their own -- the tiered engine, the
#: supplementary pass and the legacy chain's Crossref helper -- so a run that
#: touched two of them had two budgets for the same host and could serve
#: Crossref 20 requests a second against a published allowance of 10.
#:
#: First config wins, which is correct here because every caller passes the
#: same `ladder.rate_limits`. Tests that want an isolated limiter construct
#: HostRateLimiter directly.
_SHARED_LIMITER = None
_SHARED_LOCK = threading.Lock()


def shared_host_limiter(config: Optional[dict] = None) -> "HostRateLimiter":
    """The one limiter every production caller should use."""
    global _SHARED_LIMITER
    with _SHARED_LOCK:
        if _SHARED_LIMITER is None:
            _SHARED_LIMITER = HostRateLimiter(config)
        return _SHARED_LIMITER


class HostRateLimiter:
    """Resolves a host to its bucket, honoring the keyed/unkeyed distinction."""

    def __init__(self, config: Optional[dict] = None):
        self._config = dict(config or {})
        self._default = self._config.get("_default", {"rps": 3, "burst": 3})
        self._buckets: Dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def _spec_for(self, host: str) -> dict:
        spec = self._config.get(host)
        if not isinstance(spec, dict):
            return self._default
        return spec

    def bucket(self, host: Optional[str]) -> TokenBucket:
        key = host or "_default"
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is not None:
                return bucket
            spec = self._spec_for(key)
            rate = spec.get("rps", 3)
            key_env = spec.get("key_env")
            # The keyed allowance only applies if we actually hold the key.
            if key_env and os.getenv(key_env) and spec.get("rps_keyed"):
                rate = spec["rps_keyed"]
            bucket = TokenBucket(rate=rate, burst=spec.get("burst", rate))
            self._buckets[key] = bucket
            return bucket

    def acquire(self, host: Optional[str]) -> float:
        return self.bucket(host).acquire()

    def penalize(self, host: Optional[str], seconds: float) -> None:
        self.bucket(host).penalize(seconds)
