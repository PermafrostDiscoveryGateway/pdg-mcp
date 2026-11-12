"""Rate limiting that protects the Arctic Data Center from overload.

Two independent layers, both configured under ``[ratelimit]``:

* :class:`UserRateLimitMiddleware` gives each user a token bucket keyed on the Keycloak ``sub`` claim, so
  one runaway agent cannot starve everyone else. Without auth (stdio / ``--insecure``) all callers share
  one bucket.
* :class:`UpstreamLimiter` caps the number of requests in flight to ADC across all users, and computes
  backoff delays for retries on 429/502/503/504.
"""

from __future__ import annotations

import asyncio
import email.utils
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult

from arcticdata_mcp.config import RateLimitSettings

RETRY_STATUSES = frozenset({429, 502, 503, 504})
ANONYMOUS = "anonymous"


class TokenBucket:
    """Classic token bucket: ``capacity`` tokens, refilled at ``rate`` tokens per second."""

    def __init__(self, rate: float, capacity: int):
        self.rate = rate
        self.capacity = max(1, capacity)
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()

    def try_consume(self) -> float:
        """Take one token. Returns 0 on success, or the seconds to wait until one is available."""
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        return (1 - self.tokens) / self.rate


class UserRateLimitMiddleware(Middleware):
    """Reject tool calls from a user who exceeds ``per_user_rate`` (with ``per_user_burst`` headroom)."""

    def __init__(self, settings: RateLimitSettings):
        self.rate = settings.per_user_rate
        self.burst = settings.per_user_burst
        self.buckets: dict[str, TokenBucket] = {}

    @staticmethod
    def user_key() -> str:
        token = get_access_token()
        if token is None:
            return ANONYMOUS
        return str(token.claims.get("sub") or token.client_id or ANONYMOUS)

    def _prune(self) -> None:
        # Drop buckets that have fully refilled; they behave exactly like a fresh bucket.
        now = time.monotonic()
        full = [k for k, b in self.buckets.items() if b.tokens + (now - b.updated) * b.rate >= b.capacity]
        for k in full:
            del self.buckets[k]

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        if self.rate > 0:
            key = self.user_key()
            bucket = self.buckets.get(key)
            if bucket is None:
                if len(self.buckets) > 1000:
                    self._prune()
                bucket = self.buckets[key] = TokenBucket(self.rate, self.burst)
            wait = bucket.try_consume()
            if wait:
                raise ToolError(
                    f"Rate limit exceeded: at most {self.rate:g} tool calls per second "
                    f"(burst {self.burst}). Retry in {math.ceil(wait)} s."
                )
        return await call_next(context)


class UpstreamLimiter:
    """Global concurrency cap and retry policy for requests to ADC / DataONE."""

    def __init__(self, settings: RateLimitSettings):
        self.settings = settings
        self._sem = asyncio.Semaphore(settings.max_concurrent) if settings.max_concurrent > 0 else None

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold one of the ``max_concurrent`` upstream slots for the duration of the block."""
        if self._sem is None:
            yield
            return
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self.settings.queue_timeout)
        except TimeoutError as exc:
            raise ToolError("The Arctic Data Center is busy serving other requests; please retry shortly.") from exc
        try:
            yield
        finally:
            self._sem.release()

    def should_retry(self, resp: httpx.Response, attempt: int) -> bool:
        return resp.status_code in RETRY_STATUSES and attempt < self.settings.max_retries

    def delay(self, attempt: int, resp: httpx.Response | None = None) -> float:
        """Seconds to sleep before retry number ``attempt + 1``. Honors ``Retry-After`` when present."""
        retry_after = _parse_retry_after(resp.headers.get("Retry-After")) if resp is not None else None
        if retry_after is None:
            retry_after = self.settings.retry_backoff * (2**attempt)
        return max(0.0, min(retry_after, self.settings.retry_max_delay))


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return when.timestamp() - time.time()
