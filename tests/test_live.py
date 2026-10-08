"""Smoke tests against the real Arctic Data Center API. Run with: uv run pytest -m live"""

import pytest
from fastmcp import Client

from arcticdata_mcp.config import Settings
from arcticdata_mcp.server import create_server

pytestmark = pytest.mark.live
DOI = "doi:10.18739/A29K4B"


@pytest.fixture
async def live():
    async with Client(create_server(Settings(), require_auth=False)) as c:
        yield c


async def test_ping(live):
    assert (await live.call_tool("ping", {})).structured_content["up"]


async def test_search_list_download(live):
    r = await live.call_tool("search_by_keyword", {"keyword": "permafrost", "rows": 1})
    assert r.structured_content["numFound"] > 0
    files = (await live.call_tool("list_dataset_files", {"pid": DOI, "rows": 2})).structured_content
    assert files["numFound"] > 0
    small = (await live.call_tool("search_data_files", {"file_name": "*.csv", "max_size": 20000, "rows": 1})).structured_content
    pid = small["results"][0]["id"]
    dl = await live.call_tool("download_file", {"pid": pid})
    assert dl.structured_content["delivery"] == "inline"
