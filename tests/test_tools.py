import base64

import httpx
import pytest
from conftest import MN
from fastmcp import Client
from fastmcp.exceptions import ToolError

from arcticdata_mcp.server import create_server

SYSMETA = """<?xml version="1.0"?>
<ns3:systemMetadata xmlns:ns3="http://ns.dataone.org/service/types/v2.0">
  <identifier>{pid}</identifier><formatId>{fmt}</formatId><size>{size}</size>
  <checksum algorithm="MD5">abc</checksum><fileName>{name}</fileName>
  <accessPolicy><allow><subject>public</subject><permission>read</permission></allow></accessPolicy>
</ns3:systemMetadata>"""


def solr_json(docs, num=None, **extra):
    return {"response": {"numFound": len(docs) if num is None else num, "start": 0, "docs": docs}, **extra}


async def test_lists_all_tools(client):
    names = {t.name for t in await client.list_tools()}
    assert len(names) == 53
    assert {"search_datasets", "download_file", "list_dataset_files", "get_facet_counts", "search_portals"} <= names


async def test_search_by_keyword_builds_query(client, mock_api):
    route = mock_api.get(f"{MN}/query/solr/").mock(
        return_value=httpx.Response(200, json=solr_json([{"id": "doi:1", "title": "T", "origin": ["A"]}], num=5))
    )
    r = await client.call_tool("search_by_keyword", {"keyword": "permafrost", "rows": 1})
    params = route.calls.last.request.url.params
    assert 'keywordsText:"permafrost"' in params["q"]
    assert "formatType:METADATA" in params.get_list("fq")
    assert "-obsoletedBy:*" in params.get_list("fq")
    assert r.structured_content["numFound"] == 5
    res = r.structured_content["results"][0]
    assert res["authors"] == ["A"]
    assert res["landing_url"].endswith("doi%3A1")


async def test_facet_counts(client, mock_api):
    mock_api.get(f"{MN}/query/solr/").mock(
        return_value=httpx.Response(
            200,
            json={
                "response": {"numFound": 10, "docs": []},
                "facet_counts": {"facet_fields": {"formatId": ["text/csv", 7, "netCDF-4", 3]}},
            },
        )
    )
    r = await client.call_tool("get_facet_counts", {"field": "formatId"})
    assert r.structured_content["values"] == [{"value": "text/csv", "count": 7}, {"value": "netCDF-4", "count": 3}]


async def test_download_small_text_inline(client, mock_api):
    pid = "urn:uuid:small"
    mock_api.get(f"{MN}/meta/urn%3Auuid%3Asmall").mock(
        return_value=httpx.Response(200, text=SYSMETA.format(pid=pid, fmt="text/csv", size=11, name="a.csv"))
    )
    mock_api.get(f"{MN}/object/urn%3Auuid%3Asmall").mock(return_value=httpx.Response(200, content=b"a,b\n1,2\n3,4"))
    r = await client.call_tool("download_file", {"pid": pid})
    assert r.structured_content["delivery"] == "inline"
    assert r.content[1].resource.text == "a,b\n1,2\n3,4"


async def test_download_small_binary_inline_as_blob(client, mock_api):
    pid = "urn:uuid:bin"
    mock_api.get(f"{MN}/meta/urn%3Auuid%3Abin").mock(
        return_value=httpx.Response(200, text=SYSMETA.format(pid=pid, fmt="image/png", size=4, name="x.png"))
    )
    mock_api.get(f"{MN}/object/urn%3Auuid%3Abin").mock(return_value=httpx.Response(200, content=b"\x89PNG"))
    r = await client.call_tool("download_file", {"pid": pid})
    assert base64.b64decode(r.content[1].resource.blob) == b"\x89PNG"


async def test_download_large_returns_url(client, mock_api):
    pid = "urn:uuid:big"
    mock_api.get(f"{MN}/meta/urn%3Auuid%3Abig").mock(
        return_value=httpx.Response(200, text=SYSMETA.format(pid=pid, fmt="netCDF-4", size=10_000_000, name="big.nc"))
    )
    obj = mock_api.get(f"{MN}/object/urn%3Auuid%3Abig")
    r = await client.call_tool("download_file", {"pid": pid})
    assert r.structured_content["delivery"] == "url"
    assert r.structured_content["download_url"] == f"{MN}/object/urn%3Auuid%3Abig"
    assert not obj.called
    with pytest.raises(ToolError):
        await client.call_tool("download_file", {"pid": pid, "delivery": "inline"})


async def test_server_save_disabled_by_default(client, mock_api):
    pid = "urn:uuid:small"
    mock_api.get(f"{MN}/meta/urn%3Auuid%3Asmall").mock(
        return_value=httpx.Response(200, text=SYSMETA.format(pid=pid, fmt="text/csv", size=11, name="a.csv"))
    )
    with pytest.raises(ToolError, match="disabled"):
        await client.call_tool("download_file", {"pid": pid, "delivery": "server"})


async def test_dataone_error_is_mapped(client, mock_api):
    mock_api.get(f"{MN}/meta/nope").mock(
        return_value=httpx.Response(
            404,
            text=(
                '<error detailCode="1420" errorCode="404" name="NotFound"><description>No system metadata</description></error>'
            ),
        )
    )
    with pytest.raises(ToolError, match="NotFound"):
        await client.call_tool("get_system_metadata", {"pid": "nope"})


async def test_get_resource_map_parses_members(client, mock_api):
    mock_api.get(f"{MN}/query/solr/").mock(
        return_value=httpx.Response(
            200, json=solr_json([{"id": "doi:1", "formatType": "METADATA", "resourceMap": ["rm_1"]}])
        )
    )
    rdf = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:ore="http://www.openarchives.org/ore/terms/" xmlns:cito="http://purl.org/spar/cito/">
  <rdf:Description rdf:about="https://cn.dataone.org/cn/v2/resolve/rm_1#aggregation">
    <ore:aggregates rdf:resource="https://cn.dataone.org/cn/v2/resolve/doi%3A1"/>
    <ore:aggregates rdf:resource="https://cn.dataone.org/cn/v2/resolve/urn%3Auuid%3Af1"/>
  </rdf:Description>
  <rdf:Description rdf:about="https://cn.dataone.org/cn/v2/resolve/doi%3A1">
    <cito:documents rdf:resource="https://cn.dataone.org/cn/v2/resolve/urn%3Auuid%3Af1"/>
  </rdf:Description>
</rdf:RDF>"""
    mock_api.get(f"{MN}/object/rm_1").mock(return_value=httpx.Response(200, text=rdf))
    r = await client.call_tool("get_resource_map", {"pid": "doi:1"})
    sc = r.structured_content
    assert sc["members"] == ["doi:1", "urn:uuid:f1"]
    assert sc["documents"] == {"doi:1": ["urn:uuid:f1"]}


async def test_citation(client, mock_api):
    mock_api.get(f"{MN}/query/solr/").mock(
        return_value=httpx.Response(
            200,
            json=solr_json(
                [
                    {
                        "id": "doi:10.18739/X",
                        "title": "Snow.",
                        "origin": ["A B", "C D", "E F"],
                        "datePublished": "2020-05-01T00:00:00Z",
                    }
                ]
            ),
        )
    )
    r = await client.call_tool("get_dataset_citation", {"pid": "doi:10.18739/X"})
    assert r.structured_content["citation"] == "A B, C D, and E F. 2020. Snow. Arctic Data Center. doi:10.18739/X."


async def test_bounding_box_search_links_map_app(client):
    tools = {t.name: t for t in await client.list_tools()}
    uri = tools["search_by_bounding_box"].meta["ui"]["resourceUri"]
    assert uri == "ui://arcticdata/search-map.html"
    [res] = [r for r in await client.list_resources() if str(r.uri) == uri]
    assert res.mime_type == "text/html;profile=mcp-app"
    [content] = await client.read_resource(uri)
    assert "ext-apps" in content.text and "ontoolresult" in content.text


async def test_show_lakes_links_map_app_and_filters_bbox(client):
    tools = {t.name: t for t in await client.list_tools()}
    assert tools["show_lakes"].meta["ui"]["resourceUri"] == "ui://water/lake-map.html"
    everything = await client.call_tool("show_lakes", {})
    assert everything.structured_content["numFound"] == 121
    assert everything.structured_content["drained"] == 3
    with_ts = await client.call_tool("show_lakes", {"only_with_timeseries": True})
    assert with_ts.structured_content["numFound"] == 5
    assert all(lake["has_timeseries"] for lake in with_ts.structured_content["lakes"])
    boxed = await client.call_tool("show_lakes", {"north": 66.6, "south": 66.5, "east": -164.0, "west": -164.2})
    assert 0 < boxed.structured_content["numFound"] < 121
    gj = boxed.structured_content["geojson"]
    assert gj["type"] == "FeatureCollection" and gj["features"][0]["geometry"]["type"] in {"Polygon", "MultiPolygon"}
    # Polygons go to the view only, not into the model's text result.
    assert '"geometry"' not in boxed.content[0].text


async def test_show_lakes_only_drained_carries_break_attributes(client):
    r = await client.call_tool("show_lakes", {"only_drained": True})
    lakes = r.structured_content["lakes"]
    assert len(lakes) == 3
    for lake in lakes:
        assert {"date_break", "date_break_year", "pre_break_median", "NetChange_perc"} <= set(lake)
        assert isinstance(lake["date_break_year"], int)


async def test_show_lakes_rejects_partial_bbox(client):
    with pytest.raises(ToolError, match="all four"):
        await client.call_tool("show_lakes", {"north": 67})


async def test_get_lake_timeseries(client):
    tools = {t.name: t for t in await client.list_tools()}
    assert tools["get_lake_timeseries"].meta["ui"]["resourceUri"] == "ui://water/lake-timeseries.html"
    r = await client.call_tool("get_lake_timeseries", {"lake_id": "b7uefy0bvcrc"})
    d = r.structured_content
    assert d["name"].startswith("b7uefy0bvcrc | ")
    assert d["break"]["date_break"] == "2018-06-01" and d["break"]["break_method"] == "simple"
    dw = d["dw"]["data"]
    # Masked months are dropped, and vegetation is the dashboard's grouped class.
    assert len(dw["dates"]) == len(dw["water"]) < 41
    assert {"water", "vegetation", "bare", "snow_and_ice"} <= set(dw)
    assert [t["name"] for t in d["dw"]["figure"]["data"]] == ["Water", "Vegetation", "Bare", "Normalized", "Breakpoint"]
    assert d["jrc"]["figure"]["data"]
    # Figures go to the view only, not into the model's text result.
    assert '"figure"' not in r.content[0].text and '"water"' in r.content[0].text


async def test_get_lake_timeseries_unknown_lake(client):
    with pytest.raises(ToolError, match="Lakes with data"):
        await client.call_tool("get_lake_timeseries", {"lake_id": "zzzzzzzzzzzz"})


@pytest.mark.parametrize("uri", ["ui://water/lake-map.html", "ui://water/lake-timeseries.html"])
async def test_water_views_are_mcp_apps(client, uri):
    [res] = [r for r in await client.list_resources() if str(r.uri) == uri]
    assert res.mime_type == "text/html;profile=mcp-app"
    [content] = await client.read_resource(uri)
    assert "ext-apps" in content.text and "ontoolresult" in content.text


PORTALS = [
    {
        "id": "urn:uuid:p1",
        "seriesId": "urn:uuid:s1",
        "label": "PerCS-Net",
        "title": "Permafrost Coastal Systems Network",
        "abstract": "Coastal permafrost.",
        "collectionQuery": 'readPermission:"CN=percs-net"',
        "logo": "urn:uuid:logo",
    },
    {
        "id": "urn:uuid:p2",
        "seriesId": "urn:uuid:s2",
        "label": "broken",
        "title": "Broken Portal",
        "collectionQuery": 'title:"*&quot;oops',
    },
    {
        "id": "urn:uuid:p3",
        "seriesId": "urn:uuid:s3",
        "label": "TestPortal",
        "title": "Robyn's Test Portal",
        "collectionQuery": "isPartOf:x",
    },
]
PORTAL_XML = """<?xml version="1.0"?>
<por:portal xmlns:por="https://purl.dataone.org/portals-1.1.0">
  <label>PerCS-Net</label><name>Permafrost Coastal Systems Network</name><description>Coastal permafrost.</description>
  <definition><filter><field>readPermission</field><value>CN=percs-net</value></filter></definition>
  <logo><identifier>urn:uuid:logo</identifier></logo>
  <section><label>About</label><title>About us</title><content><markdown><![CDATA[# About

Long text here.]]></markdown></content></section>
  <section><label>Map</label><option><optionName>sectionType</optionName><optionValue>visualization</optionValue></option></section>
  <associatedParty><individualName><givenName>Ben</givenName><surName>Jones</surName></individualName><organizationName>UAF</organizationName></associatedParty>
  <award><title>PerCS</title><awardNumber>123</awardNumber><funderName>NSF</funderName></award>
</por:portal>"""


@pytest.fixture
def portal_api(mock_api):
    """Mock Solr: portal list, batched facet counts (the 'broken' query errors), and dataset searches."""
    counts = {'readPermission:"CN=percs-net"': 23, "isPartOf:x": 0}

    def solr(request):
        params = request.url.params
        facets = params.get_list("facet.query")
        if facets:
            if any("&quot;" in f for f in facets):
                return httpx.Response(
                    500, text='<error name="ServiceFailure"><description>SyntaxError</description></error>'
                )
            single = 'id:"doi:1"' in params["q"]
            out = {}
            for f in facets:
                key, q = f[len("{!key=") :].split("}", 1)
                n = counts[q[1:-1]]
                out[key] = min(n, 1) if single else n
            return httpx.Response(200, json={**solr_json([], num=0), "facet_counts": {"facet_queries": out}})
        if params["q"].startswith("formatId:("):
            return httpx.Response(200, json=solr_json([dict(p) for p in PORTALS]))
        if "title:" in params["q"] and "collectionQuery:" in params["q"]:
            return httpx.Response(200, json=solr_json([{"id": "urn:uuid:p1"}]))
        if params["q"].startswith('id:"doi:1"'):
            return httpx.Response(200, json=solr_json([{"id": "doi:1", "title": "T"}]))
        return httpx.Response(200, json=solr_json([{"id": "doi:1", "title": "T"}], num=23))

    route = mock_api.get(f"{MN}/query/solr/").mock(side_effect=solr)
    mock_api.get(f"{MN}/object/urn%3Auuid%3Ap1").mock(return_value=httpx.Response(200, text=PORTAL_XML))
    return route


async def test_search_portals_lists_counts_and_flags_broken(settings, portal_api):
    settings.portals.exclude_labels = ["testportal"]
    async with Client(create_server(settings, require_auth=False)) as c:
        r = (await c.call_tool("search_portals", {})).structured_content
        assert r["numFound"] == 2
        by_label = {p["label"]: p for p in r["portals"]}
        assert by_label["PerCS-Net"]["dataset_count"] == 23
        assert by_label["PerCS-Net"]["portal_url"] == "https://arcticdata.io/catalog/portals/PerCS-Net"
        assert by_label["broken"]["dataset_count"] is None
        calls = portal_api.call_count
        await c.call_tool("search_portals", {"sort": "datasets"})
        assert portal_api.call_count == calls  # served from cache


async def test_search_portals_query_uses_website_fields(client, portal_api):
    r = (await client.call_tool("search_portals", {"query": "coastal permafrost"})).structured_content
    assert [p["label"] for p in r["portals"]] == ["PerCS-Net"]
    q = portal_api.calls.last.request.url.params["q"]
    assert 'abstract:"coastal"' in q and 'awardNumber:"permafrost"' in q and " AND " in q


async def test_get_portal_parses_xml(client, portal_api):
    r = (await client.call_tool("get_portal", {"portal": "percs-net", "max_section_chars": 8})).structured_content
    assert r["logo_url"] == f"{MN}/object/urn%3Auuid%3Alogo"
    assert r["collection_query"] == 'readPermission:"CN=percs-net"'
    assert r["definition"] == [{"type": "filter", "fields": ["readPermission"], "values": ["CN=percs-net"]}]
    about, viz = r["sections"]
    assert about["markdown"] == "# About\n…" and about["markdown_chars"] > 8
    assert viz == {"label": "Map", "type": "visualization"}
    assert r["people"] == [{"name": "Ben Jones", "organization": "UAF"}]
    assert r["awards"] == [{"title": "PerCS", "awardNumber": "123", "funderName": "NSF"}]


async def test_search_portal_datasets_applies_collection_query(client, portal_api):
    r = await client.call_tool(
        "search_portal_datasets", {"portal": "urn:uuid:s1", "text": "erosion", "start_date": "2018"}
    )
    params = portal_api.calls.last.request.url.params
    assert 'readPermission:"CN=percs-net"' in params.get_list("fq")
    assert "erosion" in params["q"] and "endDate:[2018-01-01T00:00:00Z TO *]" in params["q"]
    sc = r.structured_content
    assert sc["portal"]["label"] == "PerCS-Net" and sc["numFound"] == 23
    with pytest.raises(ToolError, match="all four"):
        await client.call_tool("search_portal_datasets", {"portal": "PerCS-Net", "north": 70})
    with pytest.raises(ToolError, match="search_portals"):
        await client.call_tool("search_portal_datasets", {"portal": "nope"})


async def test_find_portals_for_dataset(client, portal_api):
    r = (await client.call_tool("find_portals_for_dataset", {"pid": "doi:1"})).structured_content
    assert [p["label"] for p in r["portals"]] == ["PerCS-Net"]
    assert r["unchecked_portals"] == ["broken"]
