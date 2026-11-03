from pathlib import Path

import httpx
import pytest
from fastmcp.exceptions import ToolError

from arcticdata_mcp import profiling
from arcticdata_mcp.config import ProfileSettings
from conftest import MN
from test_tools import SYSMETA

CSV = """site,date,Latitude,Longitude,temp_C,depth (m),count,flag,note
A,2020-06-01,68.5,-149.2,1.5,10,3,true,ok
A,2020-06-02,68.6,-149.1,2.5,-9999,4,false,ok
B,2021-07-01,69.0,-148.0,NA,12,5,true,
B,2021-07-02,69.1,-147.5,4.5,14,,false,ok
C,2022-08-01,70.0,-146.0,-9999,16,7,true,ok
"""


def profile(tmp_path, text, limits=None, encoding="utf-8", header_rows="auto"):
    path = tmp_path / "data.csv"
    path.write_bytes(text.encode(encoding))
    return profiling.profile_delimited(path, limits or ProfileSettings(), header_rows)


def col(report, name):
    return next(c for c in report["columns"] if c["name"] == name)


def test_column_types_and_missing_values(tmp_path):
    r = profile(tmp_path, CSV)
    assert r["rows_profiled"] == 5 and r["columns_total"] == 9
    assert col(r, "site")["type"] == "string" and col(r, "site")["top_values"][0] == {"value": "A", "count": 2}
    assert col(r, "count")["type"] == "integer" and col(r, "count")["null_count"] == 1
    assert col(r, "flag")["type"] == "boolean"
    assert col(r, "date")["type"] == "datetime"
    assert (col(r, "date")["min"], col(r, "date")["max"]) == ("2020-06-01T00:00:00", "2022-08-01T00:00:00")
    assert col(r, "note")["null_count"] == 1 and col(r, "note")["distinct"] == 1


def test_sentinel_is_excluded_from_stats_but_counted(tmp_path):
    temp = col(profile(tmp_path, CSV), "temp_C")
    assert temp["type"] == "float"
    assert (temp["min"], temp["max"], temp["mean"]) == (1.5, 4.5, pytest.approx(8.5 / 3))
    assert temp["null_count"] == 1 and temp["sentinel_count"] == 1 and temp["non_null"] == 3


def test_both_default_sentinels_are_missing(tmp_path):
    r = col(profile(tmp_path, "v\n5\n-9999\n-99999\n7\n"), "v")
    assert (r["min"], r["max"], r["sentinel_count"], r["non_null"]) == (5.0, 7.0, 2, 2)


def test_unit_is_guessed_from_the_column_name(tmp_path):
    r = profile(tmp_path, CSV)
    assert col(r, "temp_C")["unit_guess"] == "C" and col(r, "temp_C")["unit_source"] == "column name"
    assert col(r, "depth (m)")["unit_guess"] == "m"
    assert "unit_guess" not in col(r, "count")


def test_extents_come_from_named_columns(tmp_path):
    r = profile(tmp_path, CSV)
    spatial = r["inferred_spatial_extent"]
    assert (spatial["latitude_column"], spatial["longitude_column"]) == ("Latitude", "Longitude")
    assert spatial["bbox"] == {"north": 70.0, "south": 68.5, "east": -146.0, "west": -149.2}
    temporal = r["inferred_temporal_extent"]
    assert temporal["start"] == "2020-06-01T00:00:00" and temporal["end"] == "2022-08-01T00:00:00"


@pytest.mark.parametrize("lat,lon", [("lat", "lon"), ("latitude_dd", "long_dd"), ("LAT", "LNG"), ("decimalLatitude", "decimalLongitude")])
def test_abbreviated_coordinate_names(tmp_path, lat, lon):
    r = profile(tmp_path, f"{lat},{lon}\n60.5,-100.1\n61.0,-101.0\n")
    assert r["inferred_spatial_extent"]["bbox"]["north"] == 61.0


def test_coordinate_names_are_matched_by_word_not_substring(tmp_path):
    r = profile(tmp_path, "plate,relative_humidity,translation\n10,20,30\n11,21,31\n")
    assert r["inferred_spatial_extent"] is None


def test_out_of_range_values_are_not_coordinates(tmp_path):
    assert profile(tmp_path, "lat,lon\n200,10\n300,11\n")["inferred_spatial_extent"] is None


def test_processing_dates_are_not_data_coverage(tmp_path):
    text = ("obs_date,Processing_time,v\n2020-01-01,2024-06-01,1\n2020-02-01,2024-06-01,2\n2020-03-01,2024-06-02,3\n")
    t = profile(tmp_path, text)["inferred_temporal_extent"]
    assert [c["column"] for c in t["columns"]] == ["obs_date"]
    assert (t["start"], t["end"]) == ("2020-01-01T00:00:00", "2020-03-01T00:00:00")
    assert [c["column"] for c in t["ignored_columns"]] == ["Processing_time"] and "record was made" in t["ignored_columns"][0]["reason"]


@pytest.mark.parametrize("name", ["date_created", "LastModified", "file_generated_date", "downloaded"])
def test_record_date_names_are_ignored(tmp_path, name):
    assert profile(tmp_path, f"{name},v\n2024-01-01,1\n2024-01-02,2\n")["inferred_temporal_extent"] is None


def test_year_column_gives_a_temporal_extent(tmp_path):
    r = profile(tmp_path, "year,x\n1999,1\n2004,2\n")
    assert (r["inferred_temporal_extent"]["start"], r["inferred_temporal_extent"]["end"]) == ("1999", "2004")


def test_semicolon_latin1_and_preamble(tmp_path):
    text = "Station data export\ngenerated 2024\nname;value\nCafé;1,5\nBär;2\nZoë;3\n"
    r = profile(tmp_path, text, encoding="latin-1")
    assert r["dialect"] == {"delimiter": ";", "encoding": "latin-1", "preamble_lines_skipped": 2, "header_rows": 1}
    assert [c["name"] for c in r["columns"]] == ["name", "value"] and r["rows_profiled"] == 3


def test_utf8_bom_is_not_part_of_the_first_column_name(tmp_path):
    r = profile(tmp_path, "﻿id,v\n1,2\n3,4\n")
    assert r["columns"][0]["name"] == "id" and r["dialect"]["encoding"] == "utf-8"


def test_commas_inside_quotes_do_not_confuse_the_header(tmp_path):
    text = 'id,place,when\n1,"Fairbanks, AK",2020-01-01\n2,"Nome, AK",2020-01-02\n3,"Barrow, AK",2020-01-03\n'
    r = profile(tmp_path, text)
    assert r["dialect"]["preamble_lines_skipped"] == 0 and [c["name"] for c in r["columns"]] == ["id", "place", "when"]


def test_title_row_above_the_header_is_skipped(tmp_path):
    r = profile(tmp_path, "Sample information,,\nname,lat,lon\na,60,-100\nb,61,-101\n")
    assert r["dialect"]["preamble_lines_skipped"] == 1 and r["columns"][0]["name"] == "name"
    assert r["inferred_spatial_extent"]["bbox"]["north"] == 61.0


def test_time_of_day_is_not_a_date(tmp_path):
    r = profile(tmp_path, "day,clock\n2020-01-01,08:30\n2020-01-02,23:15:30\n2020-01-03,12:00\n")
    assert (col(r, "clock")["type"], col(r, "clock")["min"], col(r, "clock")["max"]) == ("time", "08:30:00", "23:15:30")
    assert [c["column"] for c in r["inferred_temporal_extent"]["columns"]] == ["day"]


UNITS_CSV = "site,temp,depth,flag\n,degC,m,\nA,1.5,10,x\nB,2.5,12,y\nC,3.5,14,x\n"


def test_units_row_is_detected_and_not_counted_as_data(tmp_path):
    r = profile(tmp_path, UNITS_CSV)
    assert r["dialect"]["header_rows"] == 2 and r["rows_profiled"] == 3
    assert (col(r, "temp")["type"], col(r, "temp")["unit_guess"], col(r, "temp")["unit_source"]) == ("float", "degC", "header row")
    assert col(r, "depth")["unit_guess"] == "m" and "unit_guess" not in col(r, "site")
    assert any("units" in n for n in r["notes"])


def test_units_row_beats_the_column_name_guess(tmp_path):
    r = profile(tmp_path, "depth_cm,t\n(mm),[K]\n1,2\n3,4\n5,6\n")
    assert (col(r, "depth_cm")["unit_guess"], col(r, "t")["unit_guess"]) == ("mm", "K")


def test_data_row_of_text_is_not_mistaken_for_units(tmp_path):
    r = profile(tmp_path, "site,temp\nA,<0.5\nB,2.5\nC,3.5\nD,4.5\n")
    assert r["dialect"]["header_rows"] == 1 and r["rows_profiled"] == 4


def test_header_rows_can_be_set_explicitly(tmp_path):
    r = profile(tmp_path, UNITS_CSV, header_rows=1)
    assert r["dialect"]["header_rows"] == 1 and r["rows_profiled"] == 4 and "unit_guess" not in col(r, "temp")
    r = profile(tmp_path, "a,b\nunits,units\nnote,note\n1,2\n3,4\n", header_rows=3)
    assert r["dialect"]["header_rows"] == 3 and r["rows_profiled"] == 2


def test_blank_lines_before_the_header_do_not_shift_the_skip(tmp_path):
    r = profile(tmp_path, "notes\n\nmore notes\n\nid,v\n1,2\n3,4\n")
    assert [c["name"] for c in r["columns"]] == ["id", "v"] and r["rows_profiled"] == 2


@pytest.mark.parametrize(
    "hint,fmt",
    [("mm/dd/yy hh:mm:ss", "%m/%d/%y %H:%M:%S"), ("yyyy-mm-dd", "%Y-%m-%d"), ("hh:mm", "%H:%M"),
     ("dd/mm/yyyy hh:mm", "%d/%m/%Y %H:%M"), ("YYYY-MM-DDThh:mm:ss", "%Y-%m-%dT%H:%M:%S"), ("dd.mm.yyyy", "%d.%m.%Y")],
)
def test_date_patterns_become_strptime_formats(hint, fmt):
    assert profiling.format_from_hint(hint) == fmt


@pytest.mark.parametrize("text", ["mm", "m", "degC", "mg/L", "ss", "hh", "UTC", "", "dd mm per day"])
def test_units_are_not_date_patterns(text):
    assert profiling.format_from_hint(text) is None


def test_date_pattern_in_the_units_row_is_a_format_not_a_unit(tmp_path):
    text = "Time at 310 nm,SZA\nmm/dd/yy hh:mm:ss,degree\n03/04/20 10:30:00,50.1\n12/25/20 23:59:59,60.2\n01/02/21 00:00:00,70.3\n"
    r = profile(tmp_path, text)
    when = col(r, "Time at 310 nm")
    assert when["type"] == "datetime" and when["datetime_format"] == "mm/dd/yy hh:mm:ss"
    assert "unit_guess" not in when
    assert (when["min"], when["max"]) == ("2020-03-04T10:30:00", "2021-01-02T00:00:00")
    assert col(r, "SZA")["unit_guess"] == "degree"
    assert r["inferred_temporal_extent"]["start"] == "2020-03-04T10:30:00"


def test_pattern_with_the_other_year_width_still_applies(tmp_path):
    text = "t,v\nmm/dd/yy hh:mm:ss,m\n10/28/1995 22:03:32,1\n10/29/1995 00:03:31,2\n10/30/1995 01:00:00,3\n"
    r = profile(tmp_path, text)
    assert (col(r, "t")["type"], col(r, "t")["min"], col(r, "t")["max"]) == ("datetime", "1995-10-28T22:03:32", "1995-10-30T01:00:00")
    assert col(r, "t")["datetime_format"] == "mm/dd/yy hh:mm:ss"


def test_day_first_pattern_is_not_misread_as_month_first(tmp_path):
    r = profile(tmp_path, "when,v\ndd/mm/yyyy,m\n03/04/2020,1\n25/12/2020,2\n01/02/2021,3\n")
    assert (col(r, "when")["min"], col(r, "when")["max"]) == ("2020-04-03T00:00:00", "2021-02-01T00:00:00")


def test_time_pattern_gives_a_time_column(tmp_path):
    r = profile(tmp_path, "clock,v\nhh:mm,m\n08:30,1\n23:15,2\n12:00,3\n")
    assert (col(r, "clock")["type"], col(r, "clock")["datetime_format"], col(r, "clock")["max"]) == ("time", "hh:mm", "23:15:00")


def test_tab_delimited(tmp_path):
    assert profile(tmp_path, "a\tb\n1\t2\n3\t4\n")["dialect"]["delimiter"] == "\t"


def test_chunking_gives_the_same_answer(tmp_path):
    text = "x,when\n" + "".join(f"{i},2020-01-{i % 28 + 1:02d}\n" for i in range(250))
    whole = profile(tmp_path, text)
    chunked = profile(tmp_path, text, ProfileSettings(chunk_rows=40))
    assert whole["columns"] == chunked["columns"] and chunked["rows_profiled"] == 250


def test_column_cap_lists_the_rest_by_name(tmp_path):
    r = profile(tmp_path, "a,b,c,d\n1,2,3,4\n", ProfileSettings(max_columns=2))
    assert r["columns_total"] == 4 and len(r["columns"]) == 4
    assert r["columns"][3] == {"name": "d", "profiled": False} and r["notes"]


def test_distinct_values_are_capped(tmp_path):
    r = profile(tmp_path, "id\n" + "".join(f"id{i}\n" for i in range(30)), ProfileSettings(max_distinct=10))
    assert col(r, "id")["distinct"] == 10 and col(r, "id")["distinct_is_lower_bound"] and "top_values" not in col(r, "id")


def test_all_empty_column(tmp_path):
    r = profile(tmp_path, "a,b\n1,\n2,NA\n")
    assert col(r, "b")["type"] == "empty" and col(r, "b")["null_count"] == 2


def test_binary_and_empty_files_are_rejected(tmp_path):
    with pytest.raises(ToolError, match="binary"):
        profile(tmp_path, "a,b\n\x00\x01\n")
    with pytest.raises(ToolError, match="empty"):
        profile(tmp_path, "")


def test_trim_partial_line(tmp_path):
    path = tmp_path / "t.csv"
    path.write_bytes(b"a,b\n1,2\n3,")
    profiling.trim_partial_line(path)
    assert path.read_bytes() == b"a,b\n1,2\n"


@pytest.mark.parametrize(
    "fmt,name,expected",
    [("text/csv", "a.csv", True), ("text/plain", "a.txt", True), ("text/plain", "readme.md", False),
     ("netCDF-4", "a.nc", False), ("application/zip", "a.zip", False), ("application/octet-stream", "a.dat", True),
     ("text/tsv", "a.tsv", True), ("text/tsv", "noext", True), ("application/vnd.ms-excel", "a.csv", True),
     ("application/vnd.ms-excel", "a.xls", False)],
)
def test_is_delimited(fmt, name, expected):
    assert profiling.is_delimited(fmt, name) is expected


# Tool -----------------------------------------------------------------------------------------------------------

def mock_file(mock_api, pid, fmt, name, body):
    quoted = pid.replace(":", "%3A")
    mock_api.get(f"{MN}/meta/{quoted}").mock(return_value=httpx.Response(
        200, text=SYSMETA.format(pid=pid, fmt=fmt, size=len(body), name=name)))
    return mock_api.get(f"{MN}/object/{quoted}").mock(return_value=httpx.Response(200, content=body))


async def test_profile_file_full_file(client, mock_api):
    mock_file(mock_api, "urn:uuid:csv", "text/csv", "obs.csv", CSV.encode())
    sc = (await client.call_tool("profile_file", {"pid": "urn:uuid:csv"})).structured_content
    assert sc["supported"] and sc["scan"] == "sample" and sc["complete"] and sc["rows_profiled"] == 5
    assert sc["fileName"] == "obs.csv" and sc["bytes_read"] == len(CSV.encode())
    assert sc["inferred_spatial_extent"]["bbox"]["north"] == 70.0 and "estimated_total_rows" not in sc


async def test_profile_file_sample_stops_at_the_byte_limit(client, mock_api, settings):
    body = ("x,y\n" + "".join(f"{i},{i * 2}\n" for i in range(2000))).encode()
    settings.profile.sample_bytes = 500
    mock_file(mock_api, "urn:uuid:big", "text/csv", "big.csv", body)
    sc = (await client.call_tool("profile_file", {"pid": "urn:uuid:big"})).structured_content
    assert not sc["complete"] and sc["bytes_read"] == 500 and 0 < sc["rows_profiled"] < 100
    assert sc["estimated_total_rows"] > 1000 and "scan='full'" in sc["notes"][0]
    assert col(sc, "x")["max"] < 100


async def test_profile_file_full_scan_reads_past_the_sample_limit(client, mock_api, settings):
    body = ("x,y\n" + "".join(f"{i},{i * 2}\n" for i in range(2000))).encode()
    settings.profile.sample_bytes = 500
    mock_file(mock_api, "urn:uuid:big", "text/csv", "big.csv", body)
    sc = (await client.call_tool("profile_file", {"pid": "urn:uuid:big", "scan": "full"})).structured_content
    assert sc["scan"] == "full" and sc["complete"] and sc["rows_profiled"] == 2000 and col(sc, "x")["max"] == 1999


async def test_profile_file_full_scan_is_capped_by_the_server_limit(client, mock_api, settings):
    body = ("x\n" + "".join(f"{i}\n" for i in range(2000))).encode()
    settings.profile.max_full_bytes = 400
    mock_file(mock_api, "urn:uuid:big", "text/csv", "big.csv", body)
    sc = (await client.call_tool("profile_file", {"pid": "urn:uuid:big", "scan": "full"})).structured_content
    assert not sc["complete"] and "exceeds the server's limit" in sc["notes"][0]


async def test_profile_file_reports_unsupported_formats_without_downloading(client, mock_api):
    obj = mock_file(mock_api, "urn:uuid:zip", "application/zip", "a.zip", b"PK")
    sc = (await client.call_tool("profile_file", {"pid": "urn:uuid:zip"})).structured_content
    assert sc["supported"] is False and "application/zip" in sc["reason"] and len(sc["supported_formats"]) == 3
    assert not obj.called


async def test_profile_file_header_rows_parameter(client, mock_api):
    mock_file(mock_api, "urn:uuid:u", "text/csv", "u.csv", UNITS_CSV.encode())
    auto = (await client.call_tool("profile_file", {"pid": "urn:uuid:u"})).structured_content
    one = (await client.call_tool("profile_file", {"pid": "urn:uuid:u", "header_rows": 1})).structured_content
    assert auto["dialect"]["header_rows"] == 2 and one["dialect"]["header_rows"] == 1
    with pytest.raises(ToolError):
        await client.call_tool("profile_file", {"pid": "urn:uuid:u", "header_rows": 9})


async def test_profile_file_rejects_a_bad_scan_value(client, mock_api):
    with pytest.raises(ToolError):
        await client.call_tool("profile_file", {"pid": "urn:uuid:csv", "scan": "everything"})


async def test_profile_file_is_listed_and_read_only(client):
    tools = {t.name: t for t in await client.list_tools()}
    assert tools["profile_file"].annotations.read_only_hint


@pytest.mark.parametrize(
    "fmt,name,module,func,kind",
    [("image/tiff", "map.tif", "profiling_raster", "profile_raster", "raster"),
     ("netCDF-4", "model.nc", "profiling_netcdf", "profile_netcdf", "netcdf")],
)
async def test_profile_file_hands_rasters_and_netcdf_the_object_url(
    client, mock_api, monkeypatch, settings, fmt, name, module, func, kind
):
    from arcticdata_mcp import profiling_netcdf, profiling_raster

    settings.profile.max_netcdf_download = 5  # the 10-byte file counts as too big to download

    seen = {}

    def fake(source, limits, scan):
        seen.update(source=source, scan=scan)
        return {"kind": kind, "complete": False, "notes": ["partial"]}

    async def fake_async(source, limits, scan, timeout):
        return fake(source, limits, scan)

    if module == "profiling_netcdf":
        monkeypatch.setattr(profiling_netcdf, "profile_in_subprocess", fake_async)
    else:
        monkeypatch.setattr(profiling_raster, func, fake)
    obj = mock_file(mock_api, "urn:uuid:big", fmt, name, b"x" * 10)
    sc = (await client.call_tool("profile_file", {"pid": "urn:uuid:big", "scan": "full"})).structured_content
    assert seen == {"source": f"{MN}/object/urn%3Auuid%3Abig", "scan": "full"}
    assert sc["supported"] and sc["kind"] == kind and sc["complete"] is False and sc["scan"] == "full" and sc["fileName"] == name
    assert not obj.called  # the profiler reads the file itself, over range requests


async def test_profile_file_downloads_moderate_netcdf_and_reads_it_locally(client, mock_api, monkeypatch):
    from arcticdata_mcp import profiling_netcdf

    seen = {}

    async def fake(source, limits, scan, timeout):
        seen.update(source=source, content=Path(source).read_bytes())
        return {"kind": "netcdf", "complete": True, "notes": []}

    monkeypatch.setattr(profiling_netcdf, "profile_in_subprocess", fake)
    obj = mock_file(mock_api, "urn:uuid:nc", "netCDF-4", "model.nc", b"HDF-bytes")
    sc = (await client.call_tool("profile_file", {"pid": "urn:uuid:nc"})).structured_content
    assert obj.called and seen["content"] == b"HDF-bytes" and not seen["source"].startswith("http")
    assert sc["supported"] and sc["kind"] == "netcdf" and sc["complete"] is True


async def test_profile_file_gives_up_after_the_timeout(client, mock_api, monkeypatch, settings):
    import time

    from arcticdata_mcp import profiling_raster

    monkeypatch.setattr(profiling_raster, "profile_raster", lambda *a: time.sleep(1))
    settings.profile.timeout = 0.1
    mock_file(mock_api, "urn:uuid:slow", "image/tiff", "slow.tif", b"x")
    with pytest.raises(ToolError, match="longer than"):
        await client.call_tool("profile_file", {"pid": "urn:uuid:slow"})


def test_a_long_named_column_with_non_degree_units_is_not_a_longitude(tmp_path):
    r = profile(tmp_path, "latitude,long_wave\ndeg,W/m2\n60.1,200\n61.2,250\n62.3,300\n")
    assert r["inferred_spatial_extent"] is None


def test_floats_written_with_a_decimal_point_but_all_whole_are_flagged(tmp_path):
    r = profile(tmp_path, "depth,ratio,n\n41.0,1.5,1\n52.0,2.0,2\n75.0,3.0,3\n")
    cols = {c["name"]: c for c in r["columns"]}
    assert cols["depth"]["type"] == "float" and cols["depth"]["whole_numbers"] is True
    assert "whole_numbers" not in cols["ratio"] and "whole_numbers" not in cols["n"]
