import asyncio
import json
import logging

import httpx
import pytest
from conftest import MN
from fastmcp import Client
from fastmcp.exceptions import ToolError

from arcticdata_mcp import observability
from arcticdata_mcp.client import DataONEClient
from arcticdata_mcp.config import ObservabilitySettings, load_settings
from arcticdata_mcp.observability import (
    CallRecorder,
    JsonFormatter,
    TextFormatter,
    configure_logging,
    hash_user,
)
from arcticdata_mcp.ratelimit import UserRateLimitMiddleware
from arcticdata_mcp.server import create_server

SOLR_OK = {"response": {"numFound": 0, "start": 0, "docs": []}}
SECRET_QUERY = "very-secret-query-text"


@pytest.fixture
def toolcalls(caplog):
    """Collect the structured fields of every tool_call record."""
    caplog.set_level(logging.INFO, logger=observability.toolcall_log.name)

    def records():
        return [r.fields for r in caplog.records if r.name == observability.toolcall_log.name]

    return records


async def test_successful_call_is_logged(settings, mock_api, toolcalls):
    mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(200, json=SOLR_OK))
    async with Client(create_server(settings, require_auth=False)) as c:
        await c.call_tool("count_datasets", {"text": SECRET_QUERY})
    (rec,) = toolcalls()
    assert rec["tool"] == "count_datasets"
    assert rec["status"] == "ok"
    assert rec["user_hash"] == "anonymous"
    assert rec["duration_ms"] >= 0
    assert rec["adc_call_count"] == 1
    assert rec["adc_retries"] == 0
    assert rec["adc_calls"] == [{"endpoint": "query", "method": "GET", "status": 200, "ms": rec["adc_calls"][0]["ms"]}]
    assert "error_type" not in rec


async def test_query_text_and_urls_are_never_logged(settings, mock_api, toolcalls, caplog):
    mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(200, json=SOLR_OK))
    async with Client(create_server(settings, require_auth=False)) as c:
        await c.call_tool("count_datasets", {"text": SECRET_QUERY})
    for record in caplog.records:
        assert SECRET_QUERY not in JsonFormatter().format(record)


async def test_tool_error_is_logged_without_message(settings, mock_api, toolcalls):
    mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(400, text=f"bad {SECRET_QUERY}"))
    async with Client(create_server(settings, require_auth=False)) as c:
        with pytest.raises(ToolError):
            await c.call_tool("count_datasets", {"text": SECRET_QUERY})
    (rec,) = toolcalls()
    assert rec["status"] == "error"
    assert rec["error_type"] == "ToolError"
    assert rec["adc_calls"][0]["status"] == 400
    assert SECRET_QUERY not in json.dumps(rec)


async def test_rate_limited_call_is_logged(settings, mock_api, toolcalls):
    settings.ratelimit.per_user_rate = 0.01
    settings.ratelimit.per_user_burst = 1
    mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(200, json=SOLR_OK))
    async with Client(create_server(settings, require_auth=False)) as c:
        await c.call_tool("count_datasets", {"text": "x"})
        with pytest.raises(ToolError, match="Rate limit exceeded"):
            await c.call_tool("count_datasets", {"text": "x"})
    ok, limited = toolcalls()
    assert ok["status"] == "ok"
    assert limited["status"] == "rate_limited"
    assert limited["adc_call_count"] == 0


async def test_upstream_busy_is_logged(settings, mock_api, toolcalls):
    settings.ratelimit.max_concurrent = 1
    settings.ratelimit.queue_timeout = 0.01

    async def slow(request):
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=SOLR_OK)

    mock_api.get(f"{MN}/query/solr/").mock(side_effect=slow)
    async with Client(create_server(settings, require_auth=False)) as c:
        results = await asyncio.gather(
            c.call_tool("count_datasets", {"text": "x"}),
            c.call_tool("count_datasets", {"text": "y"}),
            return_exceptions=True,
        )
    assert sum(isinstance(r, ToolError) for r in results) == 1
    assert sorted(r["status"] for r in toolcalls()) == ["ok", "upstream_busy"]


async def test_unknown_tool_and_bad_arguments_are_logged(settings, toolcalls):
    async with Client(create_server(settings, require_auth=False)) as c:
        with pytest.raises(ToolError):
            await c.call_tool("made_up_tool_name", {})
        with pytest.raises(ToolError):
            await c.call_tool("count_datasets", {"bogus": 1})
    unknown, invalid = toolcalls()
    assert (unknown["tool"], unknown["status"]) == ("unknown", "not_found")  # client-chosen name not logged
    assert (invalid["tool"], invalid["status"]) == ("count_datasets", "invalid_arguments")


async def test_retries_are_recorded(settings, mock_api, toolcalls):
    settings.ratelimit.retry_backoff = 0
    mock_api.get(f"{MN}/query/solr/").mock(
        side_effect=[httpx.Response(503), httpx.Response(429), httpx.Response(200, json=SOLR_OK)]
    )
    async with Client(create_server(settings, require_auth=False)) as c:
        await c.call_tool("count_datasets", {"text": "x"})
    (rec,) = toolcalls()
    assert rec["adc_call_count"] == 3
    assert rec["adc_retries"] == 2
    assert [c["status"] for c in rec["adc_calls"]] == [503, 429, 200]
    assert [c.get("retried", False) for c in rec["adc_calls"]] == [True, True, False]


async def test_transport_failure_is_recorded(settings, mock_api, toolcalls):
    mock_api.get(f"{MN}/query/solr/").mock(side_effect=httpx.ConnectError("boom"))
    async with Client(create_server(settings, require_auth=False)) as c:
        with pytest.raises(ToolError):
            await c.call_tool("count_datasets", {"text": "x"})
    (rec,) = toolcalls()
    assert rec["status"] == "error"
    assert rec["adc_calls"][0]["error"] == "ConnectError"
    assert "status" not in rec["adc_calls"][0]


async def test_stream_calls_are_recorded(settings, mock_api):
    settings.ratelimit.retry_backoff = 0
    mock_api.get(f"{MN}/object/urn%3Auuid%3As").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, content=b"hello")]
    )
    client = DataONEClient(settings)
    recorder = CallRecorder()
    token = observability._recorder.set(recorder)
    try:
        async with client.stream(client.object_url("urn:uuid:s")) as resp:
            await resp.aread()
    finally:
        observability._recorder.reset(token)
        await client.aclose()
    assert [(c.endpoint, c.status, c.retried) for c in recorder.calls] == [
        ("object", 503, True),
        ("object", 200, False),
    ]


async def test_no_recorder_outside_a_tool_call(settings, mock_api):
    mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(200, json=SOLR_OK))
    client = DataONEClient(settings)
    await client.solr([("q", "x")])  # must not raise
    await client.aclose()


async def test_can_be_disabled(settings, mock_api, toolcalls):
    settings.observability.log_tool_calls = False
    mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(200, json=SOLR_OK))
    async with Client(create_server(settings, require_auth=False)) as c:
        await c.call_tool("count_datasets", {"text": "x"})
    assert toolcalls() == []


async def test_user_is_hashed(settings, mock_api, toolcalls, monkeypatch):
    settings.observability.user_hash_key = "k1"
    monkeypatch.setattr(UserRateLimitMiddleware, "user_key", staticmethod(lambda: "sub-1234"))
    mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(200, json=SOLR_OK))
    async with Client(create_server(settings, require_auth=False)) as c:
        await c.call_tool("count_datasets", {"text": "x"})
        await c.call_tool("count_datasets", {"text": "y"})
    a, b = toolcalls()
    assert a["user_hash"] == b["user_hash"] == hash_user("sub-1234", "k1")
    assert "sub-1234" not in json.dumps(a)


async def test_unset_key_still_hashes_with_a_random_per_process_key(settings, mock_api, toolcalls, monkeypatch):
    assert settings.observability.user_hash_key == ""
    monkeypatch.setattr(UserRateLimitMiddleware, "user_key", staticmethod(lambda: "sub-1234"))
    mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(200, json=SOLR_OK))
    for _ in range(2):  # two "processes"
        async with Client(create_server(settings, require_auth=False)) as c:
            await c.call_tool("count_datasets", {"text": "x"})
    a, b = (r["user_hash"] for r in toolcalls())
    assert a != b  # different random keys
    assert a != hash_user("sub-1234", "") and b != hash_user("sub-1234", "")  # never the unkeyed hash


def test_zero_ms_upstream_call_keeps_ms_field(monkeypatch, caplog):
    """0 == False in Python, so a naive falsy filter would drop ms=0 from the record."""
    from arcticdata_mcp.metrics import Metrics
    from arcticdata_mcp.observability import ToolCallLogMiddleware, UpstreamCall

    caplog.set_level(logging.INFO, logger=observability.toolcall_log.name)
    mw = ToolCallLogMiddleware(ObservabilitySettings(), Metrics())
    rec = CallRecorder(calls=[UpstreamCall("query", "GET", 200, 0), UpstreamCall("query", "GET", 503, 7, retried=True)])
    mw._log("t", "ok", None, observability.time.perf_counter(), rec)
    first, second = caplog.records[-1].fields["adc_calls"]
    assert first == {"endpoint": "query", "method": "GET", "status": 200, "ms": 0}
    assert second == {"endpoint": "query", "method": "GET", "status": 503, "ms": 7, "retried": True}


def test_hash_user():
    h = hash_user("sub-1", "k")
    assert len(h) == 16 and h == hash_user("sub-1", "k")
    assert h != hash_user("sub-2", "k")
    assert h != hash_user("sub-1", "other-key")
    assert hash_user("anonymous", "k") == "anonymous"


def test_endpoint_label(settings):
    client = DataONEClient(settings)
    assert client.endpoint_label(f"{MN}/query/solr/?q=secret") == "query"
    assert client.endpoint_label(f"{MN}/object/doi%3A10.1/x") == "object"
    assert client.endpoint_label(f"{MN}/monitor/ping") == "monitor"
    assert client.endpoint_label("https://example.org/some/path?q=1") == "example.org"
    assert client.endpoint_label(f"{client.catalog_url}/doi%3A10.18739%2FA2") == "catalog"  # never the PID


def _record(**fields):
    r = logging.LogRecord("arcticdata_mcp.toolcalls", logging.INFO, __file__, 1, "tool_call", None, None)
    r.fields = fields
    return r


def test_json_formatter():
    out = json.loads(JsonFormatter().format(_record(tool="t", status="ok", adc_calls=[{"endpoint": "query"}])))
    assert out["event"] == "tool_call"
    assert out["level"] == "INFO"
    assert out["logger"] == "arcticdata_mcp.toolcalls"
    assert out["tool"] == "t" and out["adc_calls"] == [{"endpoint": "query"}]
    assert out["ts"].endswith("+00:00")


def test_json_formatter_includes_extra_attrs_and_exceptions():
    record = logging.LogRecord("arcticdata_mcp.auth", logging.WARNING, __file__, 1, "hello %s", ("x",), None)
    record.user = "u"
    out = json.loads(JsonFormatter().format(record))
    assert out["event"] == "hello x" and out["user"] == "u"


def test_text_formatter():
    line = TextFormatter().format(_record(tool="t", status="ok", duration_ms=5))
    assert "tool_call" in line and "tool=t" in line and "duration_ms=5" in line


def test_configure_logging_is_idempotent_and_validates():
    logger = logging.getLogger("arcticdata_mcp")
    before = list(logger.handlers), logger.level, logger.propagate
    try:
        configure_logging(ObservabilitySettings(log_format="text", log_level="debug"))
        configure_logging(ObservabilitySettings(log_format="json"))
        ours = [h for h in logger.handlers if getattr(h, "_arcticdata_mcp", False)]
        assert len(ours) == 1 and isinstance(ours[0].formatter, JsonFormatter)
        assert logger.level == logging.INFO and logger.propagate is False
        with pytest.raises(ValueError, match="log_format"):
            configure_logging(ObservabilitySettings(log_format="yaml"))
    finally:
        logger.handlers[:] = before[0]
        logger.setLevel(before[1])
        logger.propagate = before[2]


async def test_query_text_never_reaches_any_log_even_at_debug(settings, mock_api):
    """httpx logs full request URLs (query text included) at INFO; configure_logging must silence it."""
    import io

    saved = {n: logging.getLogger(n).level for n in ("", "httpx", "httpcore", "arcticdata_mcp")}
    ours = logging.getLogger("arcticdata_mcp")
    saved_handlers, saved_propagate = list(ours.handlers), ours.propagate
    buf = io.StringIO()
    capture = logging.StreamHandler(buf)
    root = logging.getLogger()
    try:
        configure_logging(ObservabilitySettings(log_level="DEBUG"))
        root.setLevel(logging.DEBUG)  # as if someone turned on verbose logging
        root.addHandler(capture)
        ours.addHandler(capture)
        mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(200, json=SOLR_OK))
        async with Client(create_server(settings, require_auth=False)) as c:
            await c.call_tool("count_datasets", {"text": SECRET_QUERY})
    finally:
        root.removeHandler(capture)
        ours.handlers[:] = saved_handlers
        ours.propagate = saved_propagate
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)
    assert "tool_call" in buf.getvalue()  # the capture works...
    assert SECRET_QUERY not in buf.getvalue()  # ...and the query text is absent


def test_env_override(monkeypatch):
    monkeypatch.setenv("ARCTICDATA_MCP_OBSERVABILITY_LOG_FORMAT", "text")
    monkeypatch.setenv("ARCTICDATA_MCP_OBSERVABILITY_LOG_TOOL_CALLS", "false")
    monkeypatch.setenv("ARCTICDATA_MCP_OBSERVABILITY_USER_HASH_KEY", "s3cret")
    s = load_settings().observability
    assert (s.log_format, s.log_tool_calls, s.user_hash_key) == ("text", False, "s3cret")
