from typing import get_args

import httpx
import pytest
from conftest import MN
from fastmcp.exceptions import ToolError

from arcticdata_mcp import solr
from arcticdata_mcp.tools.metadata_gaps import MissingField

DOC = {"id": "doi:1", "title": "T", "origin": ["A"], "dateUploaded": "2025-01-01T00:00:00Z"}


def gaps_response(num=3, **counts):
    return {
        "response": {"numFound": num, "start": 0, "docs": [DOC]},
        "facet_counts": {"facet_queries": {name: counts.get(name, 0) for name in solr.PRESENT_CLAUSES}},
    }


@pytest.fixture
def route(mock_api):
    return mock_api.get(f"{MN}/query/solr/").mock(
        return_value=httpx.Response(200, json=gaps_response(keywords=3, attributes=2))
    )


def test_tool_choices_match_the_solr_map():
    assert set(get_args(MissingField)) == set(solr.PRESENT_CLAUSES)


async def test_tool_is_listed_and_read_only(client):
    tools = {t.name: t for t in await client.list_tools()}
    assert tools["search_missing_metadata"].annotations.read_only_hint


async def test_missing_all_builds_one_filter_per_field(client, route):
    r = await client.call_tool("search_missing_metadata", {"missing": ["keywords", "spatial_coverage"], "rows": 1})
    params = route.calls.last.request.url.params
    fq = params.get_list("fq")
    assert {solr.EML_FORMAT_FQ, "formatType:METADATA", "-obsoletedBy:*", "-keywords:*", "-northBoundCoord:*"} <= set(fq)
    assert params["q"] == "*:*" and params["sort"] == "dateUploaded desc" and params["rows"] == "1"
    sc = r.structured_content
    assert (sc["missing"], sc["match"], sc["eml_only"], sc["numFound"]) == (
        ["keywords", "spatial_coverage"],
        "all",
        True,
        3,
    )
    assert sc["results"][0]["id"] == "doi:1"


async def test_also_missing_counts_every_field_in_one_request(client, route):
    r = await client.call_tool("search_missing_metadata", {"missing": ["keywords"]})
    params = route.calls.last.request.url.params
    queries = params.get_list("facet.query")
    assert len(route.calls) == 1 and params["facet"] == "true"
    assert "{!key=keywords}-keywords:*" in queries and "{!key=attributes}-attributeName:*" in queries
    assert len(queries) == len(solr.PRESENT_CLAUSES)
    assert r.structured_content["also_missing"]["keywords"] == 3
    assert r.structured_content["also_missing"]["attributes"] == 2
    assert set(r.structured_content["also_missing"]) == set(solr.PRESENT_CLAUSES)


async def test_missing_any_is_a_single_or_filter(client, route):
    await client.call_tool("search_missing_metadata", {"missing": ["keywords", "funding"], "match": "any"})
    fq = route.calls.last.request.url.params.get_list("fq")
    assert "(*:* AND -keywords:*) OR (*:* AND -(awardNumber:* OR funderName:* OR fundingText:*))" in fq
    assert "-keywords:*" not in fq


async def test_options_scope_the_query(client, route):
    await client.call_tool(
        "search_missing_metadata",
        {
            "missing": ["abstract"],
            "query": "permafrost",
            "eml_only": False,
            "latest_only": False,
            "sort": "oldest",
            "start": 20,
        },
    )
    params = route.calls.last.request.url.params
    fq = params.get_list("fq")
    assert params["q"] == "(permafrost)" and params["sort"] == "dateUploaded asc" and params["start"] == "20"
    assert solr.EML_FORMAT_FQ not in fq and "-obsoletedBy:*" not in fq and "-abstract:*" in fq


@pytest.mark.parametrize(
    "args", [{"missing": []}, {"missing": ["place_names"]}, {"missing": ["keywords"], "match": "some"}]
)
async def test_rejects_bad_arguments(client, route, args):
    with pytest.raises(ToolError):
        await client.call_tool("search_missing_metadata", args)
    assert not route.calls
