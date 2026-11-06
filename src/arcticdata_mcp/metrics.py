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
from http.server import HTTPServer

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    ProcessCollector,
    generate_latest,
    start_http_server,
)

from arcticdata_mcp.config import ObservabilitySettings

log = logging.getLogger(__name__)

PREFIX = "pdg_mcp"
UNKNOWN_TOOL = "unknown"  # label for tool names the server does not have, so clients cannot mint new series

TOOL_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)
UPSTREAM_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
QUEUE_BUCKETS = (0.001, 0.01, 0.05, 0.1, 0.5, 1, 5, 10, 30)


class Metrics:
    """The server's collectors plus helpers for the events that update them."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        ProcessCollector(registry=self.registry, namespace=PREFIX)  # memory, CPU, open fds (Linux)
        r = self.registry

        self.tool_calls = Counter(
            f"{PREFIX}_tool_calls_total",
            "Tool calls by tool and outcome (ok, error, rate_limited, upstream_busy, invalid_arguments, "
            "not_found, cancelled, exception).",
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
            "code, or 'error' when no response arrived. Use status=~\"4..\" or \"5..\" for failures.",
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

    # Events -------------------------------------------------------------------------------------------
    def observe_tool_call(self, tool: str, status: str, seconds: float) -> None:
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
        log.error(
            "Metrics disabled: cannot listen on %s:%s (%s)", settings.metrics_host, settings.metrics_port, exc
        )
        return None
    log.info("Serving Prometheus metrics on %s:%s/metrics", settings.metrics_host, server.server_port)
    return server

