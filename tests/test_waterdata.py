"""WaterData backends: the bundled JSON fixture and a lakes.parquet store must answer the same way."""

import json

import duckdb
import pytest
from fastmcp import Client

from arcticdata_mcp import waterdata
from arcticdata_mcp.config import Settings
from arcticdata_mcp.server import create_server
from arcticdata_mcp.waterdata import (
    JsonWaterData,
    ParquetWaterData,
    open_water_data,
    zoom_for_bbox,
)

# A box over the bundled fixture (66.35-66.79 N, 164.33-160.85 W) wide enough to be zoomed out, and a narrow one.
WIDE = (-165.0, 66.0, -160.0, 67.0)
NARROW = (-164.2, 66.5, -164.0, 66.6)
SEARCH = {"bbox": None, "only_drained": False, "only_with_timeseries": False, "limit": 50, "offset": 0}


@pytest.fixture(scope="module")
def json_data():
    return JsonWaterData("")


@pytest.fixture(scope="module")
def parquet_dir(tmp_path_factory, json_data):
    """lakes.parquet holding the bundled fixture, in the layout scripts/build_water_store.py writes."""
    d = tmp_path_factory.mktemp("water")
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE lakes (id_geohash VARCHAR, Area_start_ha DOUBLE, Area_end_ha DOUBLE, NetChange_ha DOUBLE, "
        "NetChange_perc DOUBLE, has_timeseries BOOLEAN, date_break VARCHAR, pre_break_median DOUBLE, "
        "post_break_median DOUBLE, water_change_ha DOUBLE, water_change_perc DOUBLE, date_break_year SMALLINT, "
        "date_break_month TINYINT, west DOUBLE, south DOUBLE, east DOUBLE, north DOUBLE, lon DOUBLE, lat DOUBLE, "
        "geom VARCHAR)"
    )
    for f in json_data.lakes:
        p, (w, s, e, n) = f["properties"], f["bbox"]
        con.execute(
            "INSERT INTO lakes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                p["id_geohash"], p["Area_start_ha"], p["Area_end_ha"], p["NetChange_ha"], p["NetChange_perc"],
                p["has_timeseries"], p.get("date_break"), p.get("pre_break_median"), p.get("post_break_median"),
                p.get("water_change_ha"), p.get("water_change_perc"), p.get("date_break_year"),
                p.get("date_break_month"), w, s, e, n, (w + e) / 2, (s + n) / 2, json.dumps(f["geometry"]),
            ],
        )  # fmt: skip
    con.execute(f"COPY (SELECT * FROM lakes ORDER BY id_geohash) TO '{d / 'lakes.parquet'}' (FORMAT parquet)")
    (d / "timeseries.json").write_text(json.dumps(json_data.timeseries))
    return d


@pytest.fixture(scope="module")
def parquet_data(parquet_dir):
    return ParquetWaterData(str(parquet_dir))


@pytest.fixture(params=["json", "parquet"])
def data(request, json_data, parquet_data):
    return json_data if request.param == "json" else parquet_data


def test_zoom_for_bbox():
    assert zoom_for_bbox(None) == 0
    assert zoom_for_bbox(WIDE) < waterdata.POINT_POLY_SWITCH_ZOOM <= zoom_for_bbox(NARROW)
    assert zoom_for_bbox((0, 0, 1e-9, 1e-9)) == 18


def test_open_water_data_picks_backend_from_directory(parquet_dir, tmp_path):
    assert isinstance(open_water_data(str(parquet_dir)), ParquetWaterData)
    assert isinstance(open_water_data(""), JsonWaterData)
    assert isinstance(open_water_data(str(tmp_path)), JsonWaterData)  # no lakes.parquet there


def test_counts_and_filters(data):
    r = data.search(**SEARCH)
    assert (r.num_found, r.drained) == (121, 3)
    assert data.search(**{**SEARCH, "only_drained": True}).num_found == 3
    ts = data.search(**{**SEARCH, "only_with_timeseries": True})
    assert ts.num_found == 5 and all(p["has_timeseries"] for p in ts.page)
    boxed = data.search(**{**SEARCH, "bbox": NARROW})
    assert 0 < boxed.num_found < 121


def test_ranking_puts_biggest_water_change_first(data):
    page = data.search(**SEARCH).page
    drained = [p for p in page if "date_break_year" in p]
    assert len(drained) == 3 and page[:3] == drained
    changes = [abs(p["water_change_ha"]) for p in drained if p["water_change_ha"] is not None]
    assert changes == sorted(changes, reverse=True)
    assert [p["water_change_ha"] is None for p in drained] == sorted(p["water_change_ha"] is None for p in drained)
    stable = [p["Area_start_ha"] for p in page[3:]]
    assert stable == sorted(stable, reverse=True)
    assert "date_break" not in page[3]


def test_paging(data):
    first = data.search(**{**SEARCH, "limit": 10})
    second = data.search(**{**SEARCH, "limit": 10, "offset": 10})
    allp = data.search(**{**SEARCH, "limit": 20}).page
    assert first.page + second.page == allp
    assert data.search(**{**SEARCH, "offset": 200}).page == []


def test_geometry_follows_zoom(data):
    wide = data.search(**{**SEARCH, "bbox": WIDE})
    assert wide.geometry == "point"
    assert {f["geometry"]["type"] for f in wide.features} == {"Point"}
    lon, lat = wide.features[0]["geometry"]["coordinates"]
    assert -165 < lon < -160 and 66 < lat < 67
    narrow = data.search(**{**SEARCH, "bbox": NARROW})
    assert narrow.geometry == "polygon"
    assert {f["geometry"]["type"] for f in narrow.features} <= {"Polygon", "MultiPolygon"}


def test_caps_keep_the_top_ranked_lakes(data, monkeypatch):
    monkeypatch.setattr(waterdata, "MAX_POINTS", 4)
    r = data.search(**SEARCH)
    assert r.num_found == 121 and len(r.features) == 4
    assert [f["properties"] for f in r.features] == data.search(**{**SEARCH, "limit": 4}).page


def test_lake_lookup(data):
    lake = data.lake("b7uefy0bvcrc")
    assert lake["id_geohash"] == "b7uefy0bvcrc" and "geometry" not in lake
    assert data.lake("zzzzzzzzzzzz") is None


async def test_show_lakes_tool_serves_the_parquet_store(parquet_dir):
    settings = Settings()
    settings.water.data_dir = str(parquet_dir)
    async with Client(create_server(settings, require_auth=False)) as c:
        r = await c.call_tool("show_lakes", {"limit": 10})
        sc = r.structured_content
        assert (sc["numFound"], sc["returned"], sc["next_offset"], sc["geometry"]) == (121, 10, 10, "point")
        assert sc["shown"] == 121 and sc["map_note"] is None
        assert '"geometry"' not in r.content[0].text
        ts = await c.call_tool("get_lake_timeseries", {"lake_id": "b7uefy0bvcrc"})
        assert ts.structured_content["lake"]["id_geohash"] == "b7uefy0bvcrc"


async def test_show_lakes_tool_says_when_the_map_is_capped(parquet_dir, monkeypatch):
    monkeypatch.setattr(waterdata, "MAX_POINTS", 20)
    monkeypatch.setattr("arcticdata_mcp.tools.water.MAX_POINTS", 20)
    settings = Settings()
    settings.water.data_dir = str(parquet_dir)
    async with Client(create_server(settings, require_auth=False)) as c:
        sc = (await c.call_tool("show_lakes", {})).structured_content
        assert sc["shown"] == 20 and "top 20 of 121" in sc["map_note"] and "zoom in" in sc["map_note"]
