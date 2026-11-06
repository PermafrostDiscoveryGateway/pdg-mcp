import asyncio
import logging
import socket

import httpx
import pytest
from conftest import MN
from fastmcp import Client
from fastmcp.exceptions import ToolError

from arcticdata_mcp.config import ObservabilitySettings, load_settings
from arcticdata_mcp.metrics import Metrics, start_metrics_server
from arcticdata_mcp.server import create_server

SOLR_OK = {"response": {"numFound": 0, "start": 0, "docs": []}}
SECRET_QUERY = "very-secret-query-text"


@pytest.fixture
def metrics():
    return Metrics()


def sample(metrics: Metrics, name: str, **labels) -> float:
    value = metrics.registry.get_sample_value(name, labels)
    return 0.0 if value is None else value


async def test_successful_call_is_counted(settings, mock_api, metrics):
    mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(200, json=SOLR_OK))
    async with Client(create_server(settings, require_auth=False, metrics=metrics)) as c:
        await c.call_tool("count_datasets", {"text": "x"})
    assert sample(metrics, "pdg_mcp_tool_calls_total", tool="count_datasets", status="ok") == 1
    assert sample(metrics, "pdg_mcp_tool_call_duration_seconds_count", tool="count_datasets") == 1
    assert sample(metrics, "pdg_mcp_adc_requests_total", endpoint="query", method="GET", status="200") == 1
    assert sample(metrics, "pdg_mcp_adc_request_duration_seconds_count", endpoint="query") == 1
    assert sample(metrics, "pdg_mcp_adc_queue_wait_seconds_count") == 1
    assert sample(metrics, "pdg_mcp_adc_requests_in_flight") == 0
    assert sample(metrics, "pdg_mcp_adc_requests_waiting") == 0


async def test_adc_errors_and_retries(settings, mock_api, metrics):
    settings.ratelimit.retry_backoff = 0
    mock_api.get(f"{MN}/query/solr/").mock(
        side_effect=[httpx.Response(503), httpx.Response(429), httpx.Response(200, json=SOLR_OK)]
    )
    async with Client(create_server(settings, require_auth=False, metrics=metrics)) as c:
        await c.call_tool("count_datasets", {"text": "x"})
    for status in ("503", "429", "200"):
        assert sample(metrics, "pdg_mcp_adc_requests_total", endpoint="query", method="GET", status=status) == 1
    assert sample(metrics, "pdg_mcp_adc_retries_total", endpoint="query") == 2


async def test_failed_call_and_4xx(settings, mock_api, metrics):
    mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(400, text="bad"))
    async with Client(create_server(settings, require_auth=False, metrics=metrics)) as c:
        with pytest.raises(ToolError):
            await c.call_tool("count_datasets", {"text": "x"})
    assert sample(metrics, "pdg_mcp_tool_calls_total", tool="count_datasets", status="error") == 1
    assert sample(metrics, "pdg_mcp_adc_requests_total", endpoint="query", method="GET", status="400") == 1
    assert sample(metrics, "pdg_mcp_adc_retries_total", endpoint="query") == 0


async def test_transport_failure_uses_error_status(settings, mock_api, metrics):
    mock_api.get(f"{MN}/query/solr/").mock(side_effect=httpx.ConnectError("boom"))
    async with Client(create_server(settings, require_auth=False, metrics=metrics)) as c:
        with pytest.raises(ToolError):
            await c.call_tool("count_datasets", {"text": "x"})
    assert sample(metrics, "pdg_mcp_adc_requests_total", endpoint="query", method="GET", status="error") == 1


async def test_rate_limit_rejections(settings, mock_api, metrics):
    settings.ratelimit.per_user_rate = 0.01
    settings.ratelimit.per_user_burst = 1
    mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(200, json=SOLR_OK))
    async with Client(create_server(settings, require_auth=False, metrics=metrics)) as c:
        await c.call_tool("count_datasets", {"text": "x"})
        for _ in range(2):
            with pytest.raises(ToolError, match="Rate limit exceeded"):
                await c.call_tool("count_datasets", {"text": "x"})
    assert sample(metrics, "pdg_mcp_rate_limit_rejections_total") == 2
    assert sample(metrics, "pdg_mcp_tool_calls_total", tool="count_datasets", status="rate_limited") == 2
    assert sample(metrics, "pdg_mcp_tool_calls_total", tool="count_datasets", status="ok") == 1


async def test_queue_wait_and_timeouts(settings, mock_api, metrics):
    settings.ratelimit.max_concurrent = 1
    settings.ratelimit.queue_timeout = 0.01

    async def slow(request):
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=SOLR_OK)

    mock_api.get(f"{MN}/query/solr/").mock(side_effect=slow)
    async with Client(create_server(settings, require_auth=False, metrics=metrics)) as c:
        await asyncio.gather(
            c.call_tool("count_datasets", {"text": "x"}),
            c.call_tool("count_datasets", {"text": "y"}),
            return_exceptions=True,
        )
    assert sample(metrics, "pdg_mcp_adc_queue_timeouts_total") == 1
    assert sample(metrics, "pdg_mcp_adc_queue_wait_seconds_count") == 2  # the winner and the timeout
    assert sample(metrics, "pdg_mcp_adc_queue_wait_seconds_sum") >= 0.01
    assert sample(metrics, "pdg_mcp_tool_calls_total", tool="count_datasets", status="upstream_busy") == 1
    assert sample(metrics, "pdg_mcp_adc_requests_in_flight") == 0
    assert sample(metrics, "pdg_mcp_adc_requests_waiting") == 0


async def test_unknown_tool_does_not_create_label(settings, metrics):
    async with Client(create_server(settings, require_auth=False, metrics=metrics)) as c:
        with pytest.raises(ToolError):
            await c.call_tool("made_up_tool_name", {})
    assert sample(metrics, "pdg_mcp_tool_calls_total", tool="unknown", status="not_found") == 1
    assert b"made_up_tool_name" not in metrics.render()


async def test_invalid_arguments_are_their_own_status(settings, metrics):
    async with Client(create_server(settings, require_auth=False, metrics=metrics)) as c:
        with pytest.raises(ToolError):
            await c.call_tool("count_datasets", {"bogus": 1})
    assert sample(metrics, "pdg_mcp_tool_calls_total", tool="count_datasets", status="invalid_arguments") == 1


async def test_metrics_recorded_when_logging_disabled(settings, mock_api, metrics):
    settings.observability.log_tool_calls = False
    mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(200, json=SOLR_OK))
    async with Client(create_server(settings, require_auth=False, metrics=metrics)) as c:
        await c.call_tool("count_datasets", {"text": "x"})
    assert sample(metrics, "pdg_mcp_tool_calls_total", tool="count_datasets", status="ok") == 1


async def test_exposition_has_no_query_text(settings, mock_api, metrics):
    mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(200, json=SOLR_OK))
    async with Client(create_server(settings, require_auth=False, metrics=metrics)) as c:
        await c.call_tool("count_datasets", {"text": SECRET_QUERY})
    text = metrics.render().decode()
    assert "pdg_mcp_tool_calls_total" in text
    assert SECRET_QUERY not in text


def test_metrics_are_isolated_per_instance():
    a, b = Metrics(), Metrics()
    a.observe_tool_call("t", "ok", 0.1)
    assert sample(a, "pdg_mcp_tool_calls_total", tool="t", status="ok") == 1
    assert sample(b, "pdg_mcp_tool_calls_total", tool="t", status="ok") == 0


def test_metrics_server_serves_exposition(metrics):
    metrics.observe_tool_call("t", "ok", 0.1)
    server = start_metrics_server(ObservabilitySettings(metrics_port=0), metrics)
    try:
        assert server is not None
        resp = httpx.get(f"http://127.0.0.1:{server.server_port}/metrics")
        assert resp.status_code == 200
        assert 'pdg_mcp_tool_calls_total{status="ok",tool="t"} 1.0' in resp.text
    finally:
        server.shutdown()


def test_metrics_server_can_be_disabled(metrics):
    assert start_metrics_server(ObservabilitySettings(metrics_enabled=False), metrics) is None


def test_busy_metrics_port_is_logged_not_fatal(metrics, caplog):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        with caplog.at_level(logging.ERROR, logger="arcticdata_mcp.metrics"):
            assert start_metrics_server(ObservabilitySettings(metrics_port=port), metrics) is None
    assert "Metrics disabled" in caplog.text


async def test_mcp_app_does_not_serve_metrics(settings, metrics):
    app = create_server(settings, require_auth=False, metrics=metrics).http_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/metrics")).status_code == 404


def test_env_override(monkeypatch):
    monkeypatch.setenv("ARCTICDATA_MCP_OBSERVABILITY_METRICS_HOST", "0.0.0.0")
    monkeypatch.setenv("ARCTICDATA_MCP_OBSERVABILITY_METRICS_PORT", "9200")
    monkeypatch.setenv("ARCTICDATA_MCP_OBSERVABILITY_METRICS_ENABLED", "false")
    s = load_settings().observability
    assert (s.metrics_host, s.metrics_port, s.metrics_enabled) == ("0.0.0.0", 9200, False)
