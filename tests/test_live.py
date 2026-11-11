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


async def test_get_server_config(live):
    assert (await live.call_tool("get_server_config", {})).structured_content


async def test_search_list_download(live):
    r = await live.call_tool("search_datasets", {"keyword": "permafrost", "rows": 1})
    assert r.structured_content["numFound"] > 0
    files = (await live.call_tool("list_dataset_files", {"pid": DOI, "rows": 2})).structured_content
    assert files["numFound"] > 0
    small = (await live.call_tool("search_data_files", {"file_name": "*.csv", "max_size": 20000, "rows": 1})).structured_content
    pid = small["results"][0]["id"]
    dl = await live.call_tool("download_file", {"pid": pid})
    assert dl.structured_content["delivery"] == "inline"


async def test_portals(live):
    found = (await live.call_tool("search_portals", {"query": "PerCS-Net"})).structured_content
    assert found["portals"][0]["label"] == "PerCS-Net" and found["portals"][0]["dataset_count"] > 0
    portal = (await live.call_tool("get_portal", {"portal": "PerCS-Net"})).structured_content
    assert portal["collection_query"] and portal["sections"]
    data = (await live.call_tool("search_portal_datasets", {"portal": "PerCS-Net", "rows": 1})).structured_content
    pid = data["results"][0]["id"]
    owners = (await live.call_tool("find_portals_for_dataset", {"pid": pid})).structured_content
    assert "PerCS-Net" in [p["label"] for p in owners["portals"]]
