"""Prometheus metrics, served on a separate internal port.

The metrics endpoint is deliberately *not* a route on the MCP server: the Helm Service and Ingress only
expose the MCP port, so ``/metrics`` is reachable only by scrapers that talk to the pod directly. Labels are
kept to small, server-controlled sets (tool names the server registers, ADC endpoint names, status codes);
never user IDs, PIDs or query text.

All collectors live in a per-:class:`Metrics` registry rather than the global one, so several servers can
coexist in one process (the tests do this).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from http.server import HTTPServer
from typing import TYPE_CHECKING

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    ProcessCollector,
    generate_latest,
    start_http_server,
)
from prometheus_client.core import GaugeMetricFamily

if TYPE_CHECKING:
    from arcticdata_mcp.activeusers import ActiveUsers

from arcticdata_mcp.config import ObservabilitySettings

log = logging.getLogger(__name__)

PREFIX = "pdg_mcp"
UNKNOWN_TOOL = "unknown"  # label for tool names the server does not have, so clients cannot mint new series

# Every value the ``status`` label of pdg_mcp_tool_calls_total can take.
TOOL_STATUSES = (
    "ok",
    "error",
    "rate_limited",
    "upstream_busy",
    "invalid_arguments",
    "not_found",
    "cancelled",
    "exception",
)

TOOL_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)
UPSTREAM_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
QUEUE_BUCKETS = (0.001, 0.01, 0.05, 0.1, 0.5, 1, 5, 10, 30)
ACTIVE_USERS = f"{PREFIX}_active_users"


class ActiveUsersCollector:
    """Exports ``pdg_mcp_active_users{period}``: distinct signed-in users, counted from the active-user store at
    scrape time. ``period`` is ``day`` (this UTC day so far), ``month`` (this UTC month so far), ``7d`` or ``30d``
    (rolling, in whole UTC days including today). Only counts leave the store, never hashes.

    With no store attached (not yet, or tracking is off) the metric is described but has no samples.
    """

    def __init__(self) -> None:
        self.store: ActiveUsers | None = None

    def _family(self) -> GaugeMetricFamily:
        return GaugeMetricFamily(
            ACTIVE_USERS,
            "Distinct signed-in users who made a tool call, by period: day and month are the current UTC day or "
            "month so far; 7d and 30d are rolling whole UTC days including today. Anonymous calls are not counted.",
            labels=["period"],
        )

    def describe(self) -> Iterator[GaugeMetricFamily]:
        yield self._family()

    def collect(self) -> Iterator[GaugeMetricFamily]:
        family = self._family()
        if self.store is not None:
            try:
                for period, n in self.store.counts().items():
                    family.add_metric([period], n)
            except Exception:  # a broken store must not break the whole scrape
                log.warning("Could not count active users for /metrics", exc_info=True)
        yield family


class Metrics:
    """The server's collectors plus helpers for the events that update them."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        # Tool names that may be used as labels. Until init_tools runs, every name maps to "unknown".
        self._known_tools: frozenset[str] = frozenset({UNKNOWN_TOOL})
        ProcessCollector(registry=self.registry, namespace=PREFIX)  # memory, CPU, open fds (Linux)
        r = self.registry

        self.tool_calls = Counter(
            f"{PREFIX}_tool_calls_total",
            f"Tool calls by tool and outcome ({', '.join(TOOL_STATUSES)}).",
            ["tool", "status"],
            registry=r,
        )
        self.tool_duration = Histogram(
            f"{PREFIX}_tool_call_duration_seconds",
            "Tool call latency, including time queued for an ADC slot.",
            ["tool"],
            buckets=TOOL_BUCKETS,
            registry=r,
        )
        self.rate_limit_rejections = Counter(
            f"{PREFIX}_rate_limit_rejections_total",
            "Tool calls rejected by the per-user rate limit.",
            registry=r,
        )
        self.adc_requests = Counter(
            f"{PREFIX}_adc_requests_total",
            "HTTP requests to the Arctic Data Center / DataONE, one per attempt. status is the HTTP status "
            'code, or \'error\' when no response arrived. Use status=~"4.." or "5.." for failures.',
            ["endpoint", "method", "status"],
            registry=r,
        )
        self.adc_retries = Counter(
            f"{PREFIX}_adc_retries_total",
            "ADC requests that were retried (after a 429/502/503/504).",
            ["endpoint"],
            registry=r,
        )
        self.adc_duration = Histogram(
            f"{PREFIX}_adc_request_duration_seconds",
            "Time for ADC to respond (to the response headers for streamed downloads), per attempt.",
            ["endpoint"],
            buckets=UPSTREAM_BUCKETS,
            registry=r,
        )
        self.adc_queue_wait = Histogram(
            f"{PREFIX}_adc_queue_wait_seconds",
            "Time a request waited for a free ADC slot (ratelimit.max_concurrent), including waits that "
            "ended in a timeout.",
            buckets=QUEUE_BUCKETS,
            registry=r,
        )
        self.adc_queue_timeouts = Counter(
            f"{PREFIX}_adc_queue_timeouts_total",
            "Requests that gave up waiting for an ADC slot (ratelimit.queue_timeout).",
            registry=r,
        )
        self.adc_in_flight = Gauge(
            f"{PREFIX}_adc_requests_in_flight", "Requests currently holding an ADC slot.", registry=r
        )
        self.adc_waiting = Gauge(
            f"{PREFIX}_adc_requests_waiting", "Requests currently waiting for an ADC slot.", registry=r
        )
        self.active_users = ActiveUsersCollector()
        r.register(self.active_users)

    def attach_active_users(self, store: ActiveUsers | None) -> None:
        """Export counts from ``store`` as ``pdg_mcp_active_users`` (``None`` stops exporting them)."""
        self.active_users.store = store

    def init_tools(self, tools: Iterable[str]) -> None:
        """Register the server's tool names and create every tool's series at zero.

        Registering the names bounds the ``tool`` label to them (see :meth:`tool_label`). Creating the series
        at zero matters because Prometheus's ``increase()`` / ``rate()`` cannot see the 0 -> 1 step of a series
        that first appears already at 1, so without it the first call to each tool after a restart would not
        be counted.
        """
        names = list(tools)
        self._known_tools = frozenset(names) | {UNKNOWN_TOOL}
        for tool in (*names, UNKNOWN_TOOL):
            self.tool_duration.labels(tool)
            for status in TOOL_STATUSES:
                self.tool_calls.labels(tool, status)

    def tool_label(self, name: str) -> str:
        """``name`` if it is a registered tool, otherwise ``unknown``.

        A client can send any tool name, and a call can fail (for example rate limited) before the server
        looks the tool up. Using the raw name as a label or log field would let a client create unlimited
        series, so everything that is not a registered tool is reported as ``unknown``.
        """
        return name if name in self._known_tools else UNKNOWN_TOOL

    # Events -------------------------------------------------------------------------------------------
    def observe_tool_call(self, tool: str, status: str, seconds: float) -> None:
        tool = self.tool_label(tool)  # enforce the bound here too, whatever the caller passes
        self.tool_calls.labels(tool, status).inc()
        self.tool_duration.labels(tool).observe(seconds)
        if status == "rate_limited":
            self.rate_limit_rejections.inc()

    def observe_adc_request(
        self, endpoint: str, method: str, status: int | None, seconds: float, *, retried: bool
    ) -> None:
        self.adc_requests.labels(endpoint, method, "error" if status is None else str(status)).inc()
        self.adc_duration.labels(endpoint).observe(seconds)
        if retried:
            self.adc_retries.labels(endpoint).inc()

    def render(self) -> bytes:
        """The registry in Prometheus text exposition format."""
        return generate_latest(self.registry)


def start_metrics_server(settings: ObservabilitySettings, metrics: Metrics) -> HTTPServer | None:
    """Serve ``/metrics`` on ``metrics_host:metrics_port`` in a daemon thread.

    Returns the server, or ``None`` if metrics are disabled or the port could not be bound. A busy port is
    logged as an error but does not stop the MCP server from starting.
    """
    if not settings.metrics_enabled:
        return None
    try:
        server, _thread = start_http_server(
            settings.metrics_port, addr=settings.metrics_host, registry=metrics.registry
        )
    except OSError as exc:
        log.error("Metrics disabled: cannot listen on %s:%s (%s)", settings.metrics_host, settings.metrics_port, exc)
        return None
    log.info("Serving Prometheus metrics on %s:%s/metrics", settings.metrics_host, server.server_port)
    return server
