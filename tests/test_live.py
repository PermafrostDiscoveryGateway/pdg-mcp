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
    small = (
        await live.call_tool("search_data_files", {"file_name": "*.csv", "max_size": 20000, "rows": 1})
    ).structured_content
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


async def test_profile_file(live):
    small = (
        await live.call_tool("search_data_files", {"file_name": "*.csv", "max_size": 200000, "rows": 1})
    ).structured_content
    pid = small["results"][0]["id"]
    r = (await live.call_tool("profile_file", {"pid": pid, "scan": "full"})).structured_content
    assert r["supported"] and r["complete"] and r["rows_profiled"] > 0 and r["columns"]
    nc = (
        await live.call_tool("search_data_files", {"format_id": "netCDF-4", "max_size": 2_000_000, "rows": 1})
    ).structured_content
    r = (await live.call_tool("profile_file", {"pid": nc["results"][0]["id"]})).structured_content
    assert r["supported"] and r["kind"] == "netcdf" and r["variables"]


async def test_compare_eml_vs_data(live):
    r = (
        await live.call_tool("compare_eml_vs_data", {"pid": "doi:10.18739/A2BG2HC7C", "files_per_schema": 1})
    ).structured_content
    assert r["summary"]["files_profiled"] > 0 and r["summary"]["entities"] > 0
    assert all({"kind", "severity", "message"} <= f.keys() for f in r["findings"])
