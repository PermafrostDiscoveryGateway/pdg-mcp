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


async def test_profile_file(live):
    small = (
        await live.call_tool("search_data_files", {"file_name": "*.csv", "max_size": 200000, "rows": 1})
    ).structured_content
    pid = small["results"][0]["id"]
    r = (await live.call_tool("profile_file", {"pid": pid, "scan": "full"})).structured_content
    assert r["supported"] and r["complete"] and r["rows_profiled"] > 0 and r["columns"]
    nc = (
        await live.call_tool("search_data_files", {"format_id": "netCDF-4", "max_size": 5000000, "rows": 1})
    ).structured_content
    assert (await live.call_tool("profile_file", {"pid": nc["results"][0]["id"]})).structured_content[
        "supported"
    ] is True
    parquet = (
        await live.call_tool("search_data_files", {"format_id": "application/vnd.apache.parquet", "rows": 1})
    ).structured_content
    r = (await live.call_tool("profile_file", {"pid": parquet["results"][0]["id"]})).structured_content
    assert r["supported"] and r["rows_total"] > 0 and r["columns"] and r["read"]["bytes_fetched"] <= r["size"]
    archive = (
        await live.call_tool("search_data_files", {"format_id": "application/zip", "rows": 1})
    ).structured_content
    assert (await live.call_tool("profile_file", {"pid": archive["results"][0]["id"]})).structured_content[
        "supported"
    ] is False


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
