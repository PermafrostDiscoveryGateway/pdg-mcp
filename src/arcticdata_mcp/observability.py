"""Usage tracking: one structured log record per tool call.

Each record carries the tool name, latency, outcome, a keyed hash of the Keycloak ``sub`` (never the
``sub`` itself) and a summary of the Arctic Data Center requests the call made. Tool arguments and error
messages are deliberately **not** logged, since they can contain users' query text.

The records go to the ``arcticdata_mcp.toolcalls`` logger, formatted as JSON lines (or readable text) by
:func:`configure_logging`.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import sys
import time
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import mcp.types as mt
from fastmcp.exceptions import NotFoundError, ToolError, ValidationError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult

from arcticdata_mcp.config import ObservabilitySettings
from arcticdata_mcp.metrics import UNKNOWN_TOOL, Metrics
from arcticdata_mcp.ratelimit import ANONYMOUS, RateLimitExceeded, UpstreamBusy, UserRateLimitMiddleware

LOGGER_NAME = "arcticdata_mcp"
# HTTP client libraries log the full request URL at INFO, and the URL carries the user's query text.
# Pin them so query text cannot reach the logs even if someone raises the root log level.
QUIET_LOGGERS = ("httpx", "httpcore")
toolcall_log = logging.getLogger(f"{LOGGER_NAME}.toolcalls")

MAX_LOGGED_UPSTREAM_CALLS = 25  # keeps a runaway tool from producing a huge log line
_STANDARD_RECORD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "fields"}


# --- user identity ---------------------------------------------------------------------------------------
def hash_user(user_key: str, key: str) -> str:
    """Stable, non-reversible user identifier: truncated HMAC-SHA256 of the Keycloak ``sub``."""
    if user_key == ANONYMOUS:
        return ANONYMOUS
    return hmac.new(key.encode(), user_key.encode(), hashlib.sha256).hexdigest()[:16]


# --- per-call record of requests made to ADC -------------------------------------------------------------
@dataclass
class UpstreamCall:
    endpoint: str  # e.g. "query", "object", "meta"; never the full URL, which can hold a PID or query text
    method: str
    status: int | None  # None when the request failed before a response arrived
    ms: int
    retried: bool = False  # True when this attempt was followed by a retry
    error: str | None = None  # exception class name for transport failures


@dataclass
class CallRecorder:
    calls: list[UpstreamCall] = field(default_factory=list)


_recorder: ContextVar[CallRecorder | None] = ContextVar("upstream_call_recorder", default=None)


def record_upstream(
    endpoint: str,
    method: str,
    status: int | None,
    started: float,
    *,
    retried: bool = False,
    error: str | None = None,
) -> None:
    """Note one request to ADC against the tool call currently running (no-op outside a tool call)."""
    recorder = _recorder.get()
    if recorder is not None:
        ms = round((time.perf_counter() - started) * 1000)
        recorder.calls.append(UpstreamCall(endpoint, method, status, ms, retried, error))


# --- middleware ------------------------------------------------------------------------------------------
class ToolCallLogMiddleware(Middleware):
    """Log one structured record and update the metrics for every tool call.

    Add it before the rate limiter so rejected calls are counted too.
    """

    def __init__(
        self, settings: ObservabilitySettings, metrics: Metrics | None = None, user_hash_key: str = ""
    ):
        self.metrics = metrics or Metrics()
        # With no configured key, use a random one for this process so hashes are always keyed. They then
        # differ across restarts and pods (see observability.user_hash_key in config.example.toml).
        self.key = user_hash_key or settings.user_hash_key or secrets.token_hex(32)
        self.log_enabled = settings.log_tool_calls

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        recorder = CallRecorder()
        reset = _recorder.set(recorder)
        started = time.perf_counter()
        tool = context.message.name
        status, error_type = "ok", None
        try:
            return await call_next(context)
        except RateLimitExceeded as exc:
            status, error_type = "rate_limited", type(exc).__name__
            raise
        except UpstreamBusy as exc:
            status, error_type = "upstream_busy", type(exc).__name__
            raise
        except ToolError as exc:
            status, error_type = "error", type(exc).__name__
            raise
        except NotFoundError as exc:
            # Unknown tool name: never use a client-supplied name as a metric label or log field.
            status, error_type, tool = "not_found", type(exc).__name__, UNKNOWN_TOOL
            raise
        except ValidationError as exc:
            status, error_type = "invalid_arguments", type(exc).__name__
            raise
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:
            status, error_type = "exception", type(exc).__name__
            raise
        finally:
            _recorder.reset(reset)
            self.metrics.observe_tool_call(tool, status, time.perf_counter() - started)
            if self.log_enabled:
                self._log(tool, status, error_type, started, recorder)

    def _log(
        self, tool: str, status: str, error_type: str | None, started: float, recorder: CallRecorder
    ) -> None:
        calls = recorder.calls
        fields: dict[str, Any] = {
            "tool": tool,
            "status": status,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "user_hash": hash_user(UserRateLimitMiddleware.user_key(), self.key),
            "adc_call_count": len(calls),
            "adc_retries": sum(c.retried for c in calls),
            "adc_calls": [
                {k: v for k, v in asdict(c).items() if v is not None and v is not False}  # keep ms=0
                for c in calls[:MAX_LOGGED_UPSTREAM_CALLS]
            ],
        }
        if error_type:
            fields["error_type"] = error_type
        toolcall_log.info("tool_call", extra={"fields": fields})


# --- log formatting --------------------------------------------------------------------------------------
def _record_fields(record: logging.LogRecord) -> dict[str, Any]:
    fields = dict(getattr(record, "fields", None) or {})
    for k, v in record.__dict__.items():  # plain ``extra=`` attributes
        if k not in _STANDARD_RECORD_ATTRS:
            fields[k] = v
    return fields


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, event, then the record's structured fields."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            **_record_fields(record),
        }
        if record.exc_info:
            out["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class TextFormatter(logging.Formatter):
    """Readable ``key=value`` form of the same records, for local development."""

    def format(self, record: logging.LogRecord) -> str:
        fields = " ".join(
            f"{k}={v if isinstance(v, str) else json.dumps(v, default=str)}"
            for k, v in _record_fields(record).items()
        )
        line = f"{self.formatTime(record)} {record.levelname} {record.name} {record.getMessage()} {fields}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line.rstrip()


def configure_logging(settings: ObservabilitySettings) -> None:
    """Route the ``arcticdata_mcp`` loggers to stderr (stdout carries the protocol under stdio)."""
    if settings.log_format not in {"json", "text"}:
        raise ValueError(f"observability.log_format must be 'json' or 'text', got {settings.log_format!r}")
    logger = logging.getLogger(LOGGER_NAME)
    for h in [h for h in logger.handlers if getattr(h, "_arcticdata_mcp", False)]:
        logger.removeHandler(h)  # idempotent if called twice
    handler = logging.StreamHandler(sys.stderr)
    handler._arcticdata_mcp = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter() if settings.log_format == "json" else TextFormatter())
    logger.addHandler(handler)
    logger.setLevel(settings.log_level.upper())
    logger.propagate = False
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
