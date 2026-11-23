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
# Known problems, kept out of the weekly failure until they are decided separately: the published_newest sort uses
# datePublished, which the index cannot sort on, so that sort option always returns an error; and the
# search_datasets license filter also matches licenseUrl, which the index does not mark searchable.
KNOWN_FIELD_PROBLEMS = {
    "datePublished: type date_range cannot be sorted on",
    "licenseUrl: in the index but not searchable",
}
KNOWN_BROKEN_SORTS = {"published_newest"}


@pytest.fixture
async def live():
    async with Client(create_server(Settings(), require_auth=False)) as c:
        yield c


@pytest.fixture
async def live_all_tools():
    """Like live, with the hidden low-level tools enabled too."""
    settings = Settings()
    settings.tools.exclude_tags = []
    async with Client(create_server(settings, require_auth=False)) as c:
        yield c


async def test_ping():
    settings = Settings()
    settings.tools.exclude_tags = []  # ping is a hidden low-level tool
    async with Client(create_server(settings, require_auth=False)) as c:
        assert (await c.call_tool("ping", {})).structured_content["up"]


async def test_search_list_download(live):
    r = await live.call_tool("search_datasets", {"keyword": "permafrost", "rows": 1})
    assert r.structured_content["numFound"] > 0
    files = (await live.call_tool("list_dataset_files", {"pid": DOI, "rows": 2})).structured_content
    assert files["numFound"] > 0
    small = (
        await live.call_tool("search_data_files", {"file_name": "*.csv", "max_size": 20000, "rows": 1})
    ).structured_content
    pid = small["results"][0]["id"]
    dl = await live.call_tool("download_file", {"pid": pid})
    assert dl.structured_content["delivery"] == "inline"


async def test_variable_search_finds_more_by_concept(live):
    r = (await live.call_tool("search_by_variable", {"variable": "soil depth", "rows": 0})).structured_content
    assert r["concepts_used"][0]["uri"] == "http://purl.dataone.org/odo/ECSO_00001207"
    assert r["counts"]["annotation"] > r["counts"]["free_text"] > 0


async def test_portals(live):
    found = (await live.call_tool("search_portals", {"query": "PerCS-Net"})).structured_content
    assert found["portals"][0]["label"] == "PerCS-Net" and found["portals"][0]["dataset_count"] > 0
    portal = (await live.call_tool("get_portal", {"portal": "PerCS-Net"})).structured_content
    assert portal["collection_query"] and portal["sections"]
    data = (await live.call_tool("search_portal_datasets", {"portal": "PerCS-Net", "rows": 1})).structured_content
    pid = data["results"][0]["id"]
    owners = (await live.call_tool("find_portals_for_dataset", {"pid": pid})).structured_content
    assert "PerCS-Net" in [p["label"] for p in owners["portals"]]


# ECSO ids used in current annotations that the bundled table cannot cover: they are not defined in ECSO8.owl
# at all (13 dataset annotations in November 2026, summed per concept). The www.purl gap this guards against was 164 datasets.
MAX_UNCOVERED_DATASETS = 30


async def test_bundled_ecso_table_covers_indexed_annotations():
    """Every well-formed ECSO address in current annotations (http, https or www.) should be in concepts.json.

    Malformed values (ECSO_#PAR, the pulr.dataone.org typo, ...) are curation errors to report to ADC, not
    concepts to bundle, so they are not counted.
    """
    import re

    from arcticdata_mcp import ontology
    from arcticdata_mcp.client import DataONEClient

    client = DataONEClient(Settings())
    try:
        data = await client.solr(
            [
                ("q", "sem_annotation:*ECSO_*"),
                ("fq", "-obsoletedBy:*"),
                ("fq", "formatType:METADATA"),
                ("rows", 0),
                ("facet", "true"),
                ("facet.field", "sem_annotation"),
                ("facet.limit", -1),
                ("facet.mincount", 1),
                ("json.nl", "map"),
            ]
        )
    finally:
        await client.aclose()
    uncovered: dict[str, int] = {}
    for value, n in data["facet_counts"]["facet_fields"]["sem_annotation"].items():
        uri = ontology.normalize_concept_id(value)
        if re.fullmatch(r"http://purl\.dataone\.org/odo/ECSO_\d{8}", uri) and not ontology.concept_by_uri(uri):
            uncovered[uri] = uncovered.get(uri, 0) + n
    worst = sorted(uncovered.items(), key=lambda kv: -kv[1])
    assert sum(uncovered.values()) <= MAX_UNCOVERED_DATASETS, f"ECSO concepts annotated but not bundled: {worst}"


async def test_search_missing_metadata(live):
    async def found(**args):
        return (await live.call_tool("search_missing_metadata", {"rows": 0, **args})).structured_content["numFound"]

    r = (await live.call_tool("search_missing_metadata", {"missing": ["keywords"], "rows": 2})).structured_content
    assert r["numFound"] > 0 and r["also_missing"]["keywords"] == r["numFound"]
    assert all("keywords" not in d for d in r["results"])
    both = ["keywords", "spatial_coverage"]
    assert await found(missing=both, match="any") >= await found(missing=both)  # "any" can't match fewer than "all"
    assert await found(missing=["attributes"], eml_only=False) >= await found(missing=["attributes"])


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
    ("search_datasets", {"title": "permafrost", "author": "Romanovsky", "keyword": "permafrost"}),
    ("search_datasets", {"place": "Toolik", "organization": "University of Alaska Fairbanks"}),
    ("search_datasets", {"project": "Arctic Observing Network", "funding": "1503559"}),
    ("search_datasets", {"variable": "soil temperature", "taxon": "Rangifer tarandus"}),
    ("search_datasets", {"concept_uri": "http://purl.dataone.org/odo/ECSO_00001197", "license": "Creative Commons"}),
    ("search_datasets", {"owner": "http://orcid.org/0000-0003-1846-5140"}),
    ("search_datasets", {"north": 71.5, "south": 68, "east": -141, "west": -166, "start_date": "2010"}),
    ("search_datasets", {"record_date_from": "2020", "record_date_to": "2021", "record_date_field": "dateModified"}),
    ("search_by_variable", {"variable": "soil depth"}),
    ("search_missing_metadata", {"missing": ["keywords", "funding"], "match": "any"}),
    ("search_by_provenance", {"pid": DOI}),
    ("search_data_files", {"file_name": "*.csv", "format_id": "text/csv", "min_size": 1, "documented_by": DOI}),
    ("find_dataset_by_identifier", {"identifier": DOI}),
    ("get_facet_counts", {"field": "formatId", "limit": 1}),
    ("get_dataset_citation", {"pid": DOI}),
    ("get_version_history", {"pid": DOI}),
    ("get_package_download_info", {"pid": DOI}),
    ("find_portals_for_dataset", {"pid": DOI}),
]
SORTS = ["relevance", "newest", "oldest", "published_newest", "title", "size_desc", "size_asc"]


@pytest.mark.parametrize(
    ("tool", "args"), QUERY_TOOL_CALLS, ids=[f"{t}-{i}" for i, (t, _) in enumerate(QUERY_TOOL_CALLS)]
)
async def test_query_tools_run(live_all_tools, tool, args):
    schemas = {t.name: t.input_schema for t in await live_all_tools.list_tools()}
    if "rows" in schemas[tool]["properties"]:
        args = {**args, "rows": 1}
    await live_all_tools.call_tool(tool, args)  # raises if the tool returns an error


@pytest.mark.parametrize(
    "sort",
    [
        pytest.param(s, marks=pytest.mark.xfail(strict=True, reason="index cannot sort on datePublished"))
        if s in KNOWN_BROKEN_SORTS
        else s
        for s in SORTS
    ],
)
async def test_sort_options_run(live, sort):
    assert set(SORTS) == set(solr.SORT_OPTIONS)
    await live.call_tool("search_datasets", {"query": "permafrost", "rows": 1, "sort": sort})
