"""Shared async HTTP client.

One client for the whole process, so connection pooling and HTTP/2 multiplexing
actually pay off: the second request to ``boeing.wd1.myworkdayjobs.com`` reuses
the first one's TLS session.

Responsibilities beyond plain httpx:
  * exponential backoff with jitter on transient failures
  * per-host token-bucket rate limiting
  * conditional requests (ETag / Last-Modified) plus a short-lived response
    cache, which is what makes a 2-minute polling cadence affordable
  * classifying failures into transient vs permanent so callers can react
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import httpx
import structlog

from ..config.schema import HttpSettings
from ..errors import PermanentError, TransientError
from .ratelimit import HostRateLimiter

log = structlog.get_logger(__name__)

# 408/425/429 and 5xx are worth another attempt; everything else is not.
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 507, 522, 524}


@dataclass
class CacheEntry:
    etag: Optional[str]
    last_modified: Optional[str]
    body: bytes
    headers: Dict[str, str]
    status: int
    stored_at: float


class HttpClient:
    """Wraps httpx with retry, rate limiting and revalidation."""

    def __init__(self, settings: HttpSettings) -> None:
        self.settings = settings
        self._limiter = HostRateLimiter(
            settings.requests_per_second_per_host, settings.burst_per_host
        )
        self._cache: Dict[str, CacheEntry] = {}
        self._client: Optional[httpx.AsyncClient] = None
        self.stats = {
            "requests": 0,
            "retries": 0,
            "cache_hits": 0,
            "not_modified": 0,
            "rate_limited": 0,
            "failures": 0,
        }

    async def __aenter__(self) -> HttpClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def start(self) -> None:
        if self._client is not None:
            return
        limits = httpx.Limits(
            max_connections=self.settings.max_connections,
            max_keepalive_connections=self.settings.max_keepalive_connections,
            keepalive_expiry=30.0,
        )
        timeout = httpx.Timeout(
            connect=self.settings.connect_timeout_seconds,
            read=self.settings.read_timeout_seconds,
            write=self.settings.read_timeout_seconds,
            pool=self.settings.connect_timeout_seconds,
        )
        kwargs: Dict[str, Any] = {
            "limits": limits,
            "timeout": timeout,
            "follow_redirects": True,
            "headers": {
                "User-Agent": self.settings.user_agent,
                "Accept-Language": "en-US,en;q=0.9",
            },
        }
        if self.settings.http2:
            try:
                self._client = httpx.AsyncClient(http2=True, **kwargs)
                return
            except ImportError:
                # h2 is an optional httpx extra; HTTP/1.1 works fine without it.
                log.debug("http2_unavailable_falling_back")
        self._client = httpx.AsyncClient(**kwargs)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ #

    @staticmethod
    def _host(url: str) -> str:
        return urlparse(url).netloc or url

    @staticmethod
    def _cache_key(
        method: str, url: str, body: Optional[str], params: Optional[Dict[str, Any]] = None
    ) -> str:
        """Cache identity must include the query string.

        Nearly every paginated connector varies only the query params —
        ``?from=10``, ``?offset=100``, ``?Page=2`` — while the URL stays
        constant. Keying on the URL alone made page 2 a cache hit for page 1,
        so a board was read once and then repeated until the page limit ran
        out. Sorted so equivalent params always produce the same key.
        """
        query = ""
        if params:
            query = "&".join(f"{k}={params[k]}" for k in sorted(params))
        return f"{method}:{url}?{query}:{body or ''}"

    def _backoff_delay(self, attempt: int, retry_after: Optional[float]) -> float:
        if retry_after is not None:
            return min(retry_after, self.settings.backoff_max_seconds)
        raw = self.settings.backoff_base_seconds * (2**attempt)
        capped = min(raw, self.settings.backoff_max_seconds)
        # Full jitter. Without it, N companies sharing a host that just
        # returned 503 would retry in lockstep and re-trigger it.
        return random.uniform(0.0, capped)

    @staticmethod
    def _retry_after(response: httpx.Response) -> Optional[float]:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            try:
                from email.utils import parsedate_to_datetime

                dt = parsedate_to_datetime(value)
                return max(0.0, (dt.timestamp() - time.time()))
            except Exception:
                return None

    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[Any] = None,
        data: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        use_cache: bool = True,
        timeout: Optional[float] = None,
    ) -> Tuple[httpx.Response, bool]:
        """Perform a request. Returns ``(response, served_from_cache)``.

        Raises :class:`TransientError` when retries are exhausted and
        :class:`PermanentError` for statuses that will never succeed.
        """
        if self._client is None:
            await self.start()
        assert self._client is not None

        host = self._host(url)
        cache_key = self._cache_key(method, url, str(json_body or data or ""), params)
        cached = self._cache.get(cache_key) if use_cache else None

        # Fresh enough to skip the network entirely.
        if cached and self.settings.cache_ttl_seconds > 0:
            if time.time() - cached.stored_at < self.settings.cache_ttl_seconds:
                self.stats["cache_hits"] += 1
                return self._response_from_cache(cached, url), True

        req_headers = dict(headers or {})
        if cached and self.settings.enable_conditional_requests:
            if cached.etag:
                req_headers["If-None-Match"] = cached.etag
            if cached.last_modified:
                req_headers["If-Modified-Since"] = cached.last_modified

        last_error: Optional[Exception] = None
        for attempt in range(self.settings.max_retries + 1):
            await self._limiter.acquire(host)
            self.stats["requests"] += 1
            try:
                response = await self._client.request(
                    method,
                    url,
                    json=json_body,
                    data=data,
                    headers=req_headers,
                    params=params,
                    timeout=timeout,
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_error = exc
                if attempt >= self.settings.max_retries:
                    break
                self.stats["retries"] += 1
                delay = self._backoff_delay(attempt, None)
                log.debug("http_retry", url=url, attempt=attempt + 1, delay=round(delay, 2),
                          error=type(exc).__name__)
                await asyncio.sleep(delay)
                continue
            except httpx.HTTPError as exc:
                self.stats["failures"] += 1
                raise TransientError(f"{type(exc).__name__}: {exc}") from exc

            # The server says nothing changed, so reuse what we already parsed.
            if response.status_code == 304 and cached is not None:
                self.stats["not_modified"] += 1
                cached.stored_at = time.time()
                return self._response_from_cache(cached, url), True

            if response.status_code in RETRYABLE_STATUS:
                retry_after = self._retry_after(response)
                if response.status_code == 429:
                    self.stats["rate_limited"] += 1
                    # Slow every future request to this host, not just ours.
                    self._limiter.penalise(host, retry_after or 30.0)
                if attempt >= self.settings.max_retries:
                    self.stats["failures"] += 1
                    raise TransientError(
                        f"HTTP {response.status_code} for {url} after "
                        f"{attempt + 1} attempts",
                        retry_after=retry_after,
                    )
                self.stats["retries"] += 1
                delay = self._backoff_delay(attempt, retry_after)
                log.debug("http_retry_status", url=url, status=response.status_code,
                          attempt=attempt + 1, delay=round(delay, 2))
                await asyncio.sleep(delay)
                continue

            if 400 <= response.status_code < 500:
                self.stats["failures"] += 1
                raise PermanentError(f"HTTP {response.status_code} for {url}")

            if use_cache:
                self._store(cache_key, response)
            return response, False

        self.stats["failures"] += 1
        raise TransientError(
            f"request to {url} failed after {self.settings.max_retries + 1} attempts: "
            f"{type(last_error).__name__ if last_error else 'unknown'}"
        )

    def _store(self, key: str, response: httpx.Response) -> None:
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")
        if not etag and not last_modified and self.settings.cache_ttl_seconds <= 0:
            return
        # Skip very large bodies; the cache is for revalidation, not storage.
        if len(response.content) > 8_000_000:
            return
        # `response.content` is already decompressed. Replaying the original
        # Content-Encoding alongside it would make httpx try to gunzip
        # plaintext ("incorrect header check"), and a stale Content-Length
        # would disagree with the decoded body.
        headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
        }
        self._cache[key] = CacheEntry(
            etag=etag,
            last_modified=last_modified,
            body=response.content,
            headers=headers,
            status=response.status_code,
            stored_at=time.time(),
        )

    @staticmethod
    def _response_from_cache(entry: CacheEntry, url: str) -> httpx.Response:
        return httpx.Response(
            status_code=entry.status,
            content=entry.body,
            headers=entry.headers,
            request=httpx.Request("GET", url),
        )

    async def get_json(self, url: str, **kwargs: Any) -> Tuple[Any, bool]:
        response, cached = await self.request("GET", url, **kwargs)
        return self._parse_json(response, url), cached

    async def post_json(self, url: str, body: Any, **kwargs: Any) -> Tuple[Any, bool]:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("Accept", "application/json")
        response, cached = await self.request(
            "POST", url, json_body=body, headers=headers, **kwargs
        )
        return self._parse_json(response, url), cached

    async def get_text(self, url: str, **kwargs: Any) -> Tuple[str, bool]:
        response, cached = await self.request("GET", url, **kwargs)
        return response.text, cached

    @staticmethod
    def _parse_json(response: httpx.Response, url: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            snippet = response.text[:200].replace("\n", " ")
            # A career site that quietly serves an HTML error page is the most
            # common way a connector breaks, so name it explicitly.
            raise TransientError(
                f"expected JSON from {url} but got "
                f"{response.headers.get('content-type', 'unknown')}: {snippet}"
            ) from exc

    def cache_info(self) -> Dict[str, Any]:
        return {
            "entries": len(self._cache),
            "throttled_hosts": self._limiter.stats(),
            **self.stats,
        }

    def clear_cache(self) -> None:
        self._cache.clear()
