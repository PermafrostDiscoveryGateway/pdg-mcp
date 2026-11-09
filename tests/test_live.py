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
        data = await client.solr([
            ("q", "sem_annotation:*ECSO_*"), ("fq", "-obsoletedBy:*"), ("fq", "formatType:METADATA"), ("rows", 0),
            ("facet", "true"), ("facet.field", "sem_annotation"), ("facet.limit", -1), ("facet.mincount", 1),
            ("json.nl", "map"),
        ])
    finally:
        await client.aclose()
    uncovered: dict[str, int] = {}
    for value, n in data["facet_counts"]["facet_fields"]["sem_annotation"].items():
        uri = ontology.normalize_concept_id(value)
        if re.fullmatch(r"http://purl\.dataone\.org/odo/ECSO_\d{8}", uri) and not ontology.concept_by_uri(uri):
            uncovered[uri] = uncovered.get(uri, 0) + n
    worst = sorted(uncovered.items(), key=lambda kv: -kv[1])
    assert sum(uncovered.values()) <= MAX_UNCOVERED_DATASETS, f"ECSO concepts annotated but not bundled: {worst}"
