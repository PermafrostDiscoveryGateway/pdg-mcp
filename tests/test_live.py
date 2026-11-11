"""Smoke tests against the real Arctic Data Center API. Run with: uv run pytest -m live"""

import pytest
from fastmcp import Client

from arcticdata_mcp import solr
from arcticdata_mcp.client import DataONEClient
from arcticdata_mcp.config import Settings
from arcticdata_mcp.server import create_server
from arcticdata_mcp.tools import fields_in_use

pytestmark = pytest.mark.live
DOI = "doi:10.18739/A29K4B"
# Known problem, kept out of the weekly failure until it is decided separately: the published_newest sort uses
# datePublished, which the index cannot sort on, so that sort option always returns an error.
KNOWN_FIELD_PROBLEMS = {"datePublished: type date_range cannot be sorted on"}
KNOWN_BROKEN_SORTS = {"published_newest"}


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


async def test_portals(live):
    found = (await live.call_tool("search_portals", {"query": "PerCS-Net"})).structured_content
    assert found["portals"][0]["label"] == "PerCS-Net" and found["portals"][0]["dataset_count"] > 0
    portal = (await live.call_tool("get_portal", {"portal": "PerCS-Net"})).structured_content
    assert portal["collection_query"] and portal["sections"]
    data = (await live.call_tool("search_portal_datasets", {"portal": "PerCS-Net", "rows": 1})).structured_content
    pid = data["results"][0]["id"]
    owners = (await live.call_tool("find_portals_for_dataset", {"pid": pid})).structured_content
    assert "PerCS-Net" in [p["label"] for p in owners["portals"]]


async def test_field_names_match_the_index():
    """Every Solr field name we hard-code still exists in the index with the flag we rely on (#28)."""
    client = DataONEClient(Settings())
    try:
        index = await solr.fetch_index_fields(client)
    finally:
        await client.aclose()
    assert len(index) > 100, "the field description looks truncated"
    problems = solr.check_fields(index, fields_in_use())
    fixed = KNOWN_FIELD_PROBLEMS - set(problems)
    assert not fixed, f"no longer a problem, remove from KNOWN_FIELD_PROBLEMS: {sorted(fixed)}"
    problems = [p for p in problems if p not in KNOWN_FIELD_PROBLEMS]
    assert not problems, "Solr field names out of sync with the index:\n" + "\n".join(problems)


# One simple call per tool that builds a query, to catch a field the lists in fields_in_use() miss:
# an unknown field in q, fq or sort makes the index return an error, which fails the call.
QUERY_TOOL_CALLS = [
    ("search_datasets", {"query": "permafrost"}),
    ("search_by_title", {"title": "permafrost"}),
    ("search_by_author", {"name": "Romanovsky"}),
    ("search_by_keyword", {"keyword": "permafrost"}),
    ("search_by_bounding_box", {"north": 71.5, "south": 68, "east": -141, "west": -166}),
    ("search_by_temporal_coverage", {"start_date": "2010", "end_date": "2012"}),
    ("search_by_publication_date", {"start_date": "2020", "end_date": "2021", "date_field": "dateModified"}),
    ("search_by_funding", {"award": "1503559"}),
    ("search_by_project", {"project": "Arctic Observing Network"}),
    ("search_by_measured_variable", {"variable": "soil temperature"}),
    ("search_by_taxon", {"taxon": "Rangifer tarandus"}),
    ("search_by_semantic_annotation", {"concept_uri": "http://purl.dataone.org/odo/ECSO_00001197"}),
    ("search_by_location_name", {"place": "Toolik"}),
    ("search_by_organization", {"organization": "University of Alaska Fairbanks"}),
    ("search_by_license", {"license": "Creative Commons"}),
    ("search_by_rights_holder", {"subject": "http://orcid.org/0000-0003-1846-5140"}),
    ("search_by_provenance", {"pid": DOI}),
    ("search_by_format", {"format_id": "text/csv"}),
    ("search_data_files", {"file_name": "*.csv", "format_id": "text/csv", "min_size": 1, "documented_by": DOI}),
    ("find_dataset_by_identifier", {"identifier": DOI}),
    ("count_datasets", {"keyword": "permafrost", "author": "Romanovsky", "start_date": "2015"}),
    ("get_facet_counts", {"field": "formatId", "limit": 1}),
    ("get_dataset_citation", {"pid": DOI}),
    ("get_version_history", {"pid": DOI}),
    ("get_package_download_info", {"pid": DOI}),
]
SORTS = ["relevance", "newest", "oldest", "published_newest", "title", "size_desc", "size_asc"]


@pytest.mark.parametrize(("tool", "args"), QUERY_TOOL_CALLS, ids=[t for t, _ in QUERY_TOOL_CALLS])
async def test_query_tools_run(live, tool, args):
    schemas = {t.name: t.inputSchema for t in await live.list_tools()}
    if "rows" in schemas[tool]["properties"]:
        args = {**args, "rows": 1}
    await live.call_tool(tool, args)  # raises if the tool returns an error


@pytest.mark.parametrize("sort", [
    pytest.param(s, marks=pytest.mark.xfail(strict=True, reason="index cannot sort on datePublished"))
    if s in KNOWN_BROKEN_SORTS else s
    for s in SORTS
])
async def test_sort_options_run(live, sort):
    assert set(SORTS) == set(solr.SORT_OPTIONS)
    await live.call_tool("search_datasets", {"query": "permafrost", "rows": 1, "sort": sort})
