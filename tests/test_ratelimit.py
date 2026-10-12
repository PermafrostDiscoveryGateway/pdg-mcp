import asyncio

import httpx
import pytest
from conftest import MN
from fastmcp import Client
from fastmcp.exceptions import ToolError

from arcticdata_mcp.config import RateLimitSettings, load_settings
from arcticdata_mcp.ratelimit import TokenBucket, UpstreamLimiter
from arcticdata_mcp.server import create_server

SOLR_OK = {"response": {"numFound": 0, "start": 0, "docs": []}}


@pytest.fixture
def fast_retries(settings):
    settings.ratelimit.retry_backoff = 0
    return settings


def test_env_override(monkeypatch):
    monkeypatch.setenv("ARCTICDATA_MCP_RATELIMIT_PER_USER_RATE", "0.5")
    monkeypatch.setenv("ARCTICDATA_MCP_RATELIMIT_MAX_CONCURRENT", "2")
    s = load_settings()
    assert s.ratelimit.per_user_rate == 0.5
    assert s.ratelimit.max_concurrent == 2


def test_token_bucket_burst_then_wait():
    b = TokenBucket(rate=1.0, capacity=2)
    assert b.try_consume() == 0
    assert b.try_consume() == 0
    assert 0 < b.try_consume() <= 1.0


def test_retry_after_header_wins():
    lim = UpstreamLimiter(RateLimitSettings(retry_backoff=1.0, retry_max_delay=30))
    assert lim.delay(2) == 4.0
    assert lim.delay(0, httpx.Response(429, headers={"Retry-After": "7"})) == 7.0
    assert lim.delay(0, httpx.Response(429, headers={"Retry-After": "999"})) == 30


async def test_per_user_limit_rejects_after_burst(settings, mock_api):
    settings.ratelimit.per_user_rate = 0.01
    settings.ratelimit.per_user_burst = 2
    mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(200, json=SOLR_OK))
    async with Client(create_server(settings, require_auth=False)) as c:
        await c.call_tool("count_datasets", {"text": "x"})
        await c.call_tool("count_datasets", {"text": "x"})
        with pytest.raises(ToolError, match="Rate limit exceeded"):
            await c.call_tool("count_datasets", {"text": "x"})
        # Listing tools is not a tool call and is not limited.
        assert await c.list_tools()


async def test_per_user_limit_disabled(settings, mock_api):
    settings.ratelimit.per_user_rate = 0
    settings.ratelimit.per_user_burst = 1
    mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(200, json=SOLR_OK))
    async with Client(create_server(settings, require_auth=False)) as c:
        for _ in range(5):
            await c.call_tool("count_datasets", {"text": "x"})


async def test_retries_on_503_then_succeeds(fast_retries, mock_api):
    route = mock_api.get(f"{MN}/query/solr/").mock(
        side_effect=[httpx.Response(503), httpx.Response(429), httpx.Response(200, json=SOLR_OK)]
    )
    async with Client(create_server(fast_retries, require_auth=False)) as c:
        r = await c.call_tool("count_datasets", {"text": "x"})
    assert route.call_count == 3
    assert r.structured_content is not None


async def test_gives_up_after_max_retries(fast_retries, mock_api):
    fast_retries.ratelimit.max_retries = 2
    route = mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(503))
    async with Client(create_server(fast_retries, require_auth=False)) as c:
        with pytest.raises(ToolError, match="503"):
            await c.call_tool("count_datasets", {"text": "x"})
    assert route.call_count == 3


async def test_stream_retries(fast_retries, mock_api):
    pid = "urn:uuid:s"
    route = mock_api.get(f"{MN}/object/urn%3Auuid%3As").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, content=b"hello")]
    )
    from arcticdata_mcp.client import DataONEClient

    client = DataONEClient(fast_retries)
    async with client.stream(client.object_url(pid)) as resp:
        assert await resp.aread() == b"hello"
    await client.aclose()
    assert route.call_count == 2


async def test_global_concurrency_cap():
    lim = UpstreamLimiter(RateLimitSettings(max_concurrent=2, queue_timeout=5))
    active = peak = 0

    async def work():
        nonlocal active, peak
        async with lim.slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    await asyncio.gather(*(work() for _ in range(8)))
    assert peak == 2


async def test_queue_timeout():
    lim = UpstreamLimiter(RateLimitSettings(max_concurrent=1, queue_timeout=0.01))
    async with lim.slot():
        with pytest.raises(ToolError, match="busy"):
            async with lim.slot():
                pass
