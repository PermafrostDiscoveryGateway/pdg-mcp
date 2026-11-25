import datetime as dt
import json
import re

import httpx
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from conftest import MN
from fastmcp.exceptions import ToolError
from test_tools import SYSMETA

from arcticdata_mcp import profiling_parquet
from arcticdata_mcp.config import ProfileSettings

URL = "https://files.example/data.parquet"


def make_table(n=1000):
    i = np.arange(n)
    temp = np.where(i % 10 == 0, -9999.0, i / 10)
    return pa.table(
        {
            "site": pa.array([f"S{k % 3}" for k in i]),
            "latitude": pa.array(60 + i / n * 10),
            "longitude": pa.array(-150 + i / n * 20),
            "temp_C": pa.array(temp, mask=(i % 50 == 1)),
            "count": pa.array(i, pa.int32()),
            "flag": pa.array(i % 2 == 0),
            "when": pa.array([dt.datetime(2020, 1, 1) + dt.timedelta(days=int(k)) for k in i], pa.timestamp("ms")),
            "day": pa.array([f"2021-{1 + k % 12:02d}-15" for k in i]),
            "tags": pa.array([[int(k)] for k in i]),
        }
    )


def write(path, table=None, **options):
    pq.write_table(make_table() if table is None else table, path, **options)
    return str(path)


def profile(source, scan="sample", size=None, **limits):
    return profiling_parquet.profile_parquet(source, ProfileSettings(**limits), scan, size)


def col(report, name):
    return next(c for c in report["columns"] if c["name"] == name)


def serve(router, path, url=URL):
    """Serve a local file at ``url`` the way the archive does: range requests only, total size not disclosed."""
    body = path.read_bytes()

    def respond(request):
        start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers["range"]).groups())
        return httpx.Response(206, content=body[start : end + 1], headers={"Content-Range": f"bytes {start}-{end}/*"})

    router.head(url).mock(return_value=httpx.Response(200, headers={"Content-Length": str(len(body))}))
    return router.get(url).mock(side_effect=respond), len(body)


def test_recognizes_parquet_by_format_or_extension():
    assert profiling_parquet.is_parquet("application/vnd.apache.parquet", "a.bin")
    assert profiling_parquet.is_parquet("application/octet-stream", "a.PARQUET")
    assert not profiling_parquet.is_parquet("text/csv", "a.parquet")
    assert not profiling_parquet.is_parquet(None, "a.csv")


def test_small_file_is_profiled_completely(tmp_path):
    r = profile(write(tmp_path / "a.parquet"))
    assert (r["kind"], r["format"], r["rows_total"], r["rows_profiled"], r["columns_total"]) == (
        "table",
        "parquet",
        1000,
        1000,
        9,
    )
    assert r["complete"] is True and r["geometry"] is None and r["notes"] == []
    site = col(r, "site")
    assert site["type"] == "string" and site["distinct"] == 3 and site["top_values"][0] == {"value": "S0", "count": 334}
    count = col(r, "count")
    assert (count["type"], count["arrow_type"], count["min"], count["max"]) == ("integer", "int32", 0, 999)
    assert count["mean"] == 499.5 and count["range_from"] == "rows read"
    assert col(r, "flag")["type"] == "boolean" and col(r, "flag")["true_count"] == 500
    assert col(r, "tags")["type"] == "nested" and "min" not in col(r, "tags")


def test_sentinels_and_nulls_are_counted_and_left_out_of_statistics(tmp_path):
    temp = col(profile(write(tmp_path / "a.parquet")), "temp_C")
    assert (temp["null_count"], temp["sentinel_count"], temp["non_null"]) == (20, 100, 980)
    assert temp["min"] == 0.2 and temp["max"] == 99.9
    assert temp["unit_guess"] == "C" and temp["unit_source"] == "column name"


def test_nan_values_are_counted_as_missing(tmp_path):
    table = pa.table({"v": pa.array([1.0, float("nan"), 3.0, float("nan")])})
    v = col(profile(write(tmp_path / "n.parquet", table)), "v")
    assert (v["nan_count"], v["min"], v["max"], v["mean"]) == (2, 1.0, 3.0, 2.0)


def test_large_integers_keep_their_exact_range(tmp_path):
    big = 2**62 + 1
    v = col(profile(write(tmp_path / "i.parquet", pa.table({"v": pa.array([big, 5], pa.int64())}))), "v")
    assert (v["min"], v["max"]) == (5, big)


def test_extents_come_from_coordinate_and_time_columns(tmp_path):
    r = profile(write(tmp_path / "a.parquet"))
    box = r["inferred_spatial_extent"]["bbox"]
    assert (box["south"], box["west"]) == (60.0, -150.0) and box["north"] == pytest.approx(69.99)
    when = col(r, "when")
    assert (when["type"], when["min"], when["max"]) == ("datetime", "2020-01-01T00:00:00", "2022-09-26T00:00:00")
    assert r["inferred_temporal_extent"]["start"] == "2020-01-01T00:00:00"


def test_dates_stored_as_text_are_recognized(tmp_path):
    day = col(profile(write(tmp_path / "a.parquet")), "day")
    assert (day["type"], day["arrow_type"]) == ("datetime", "string")
    assert (day["min"], day["max"]) == ("2021-01-15T00:00:00", "2021-12-15T00:00:00")
    assert "distinct" not in day


def test_sample_of_a_big_file_gets_exact_ranges_from_the_footer(tmp_path):
    path = write(tmp_path / "a.parquet", row_group_size=100, compression="none")
    r = profile(path, sample_bytes=4000)
    assert r["row_groups"] == 10 and r["rows_total"] == 1000 and r["complete"] is False
    count = col(r, "count")
    assert count["rows_read"] < 1000  # only part of the column was read ...
    assert (count["min"], count["max"], count["range_from"]) == (0, 999, "footer")  # ... but the range is exact
    assert count["null_count"] == 0 and count["non_null"] == 1000
    temp = col(r, "temp_C")
    assert temp["min"] == -9999.0 and "missing-value code" in temp["range_note"]
    assert temp["null_count"] == 20
    assert col(r, "when")["max"] == "2022-09-26T00:00:00"
    assert "footer" in r["notes"][0] and "scan='full'" in r["notes"][0]


def test_full_scan_reads_everything_within_the_limit(tmp_path):
    path = write(tmp_path / "a.parquet", row_group_size=100)
    r = profile(path, "full", sample_bytes=4000)
    assert r["complete"] is True and r["rows_profiled"] == 1000
    assert col(r, "temp_C")["min"] == 0.2 and col(r, "temp_C")["range_from"] == "rows read"


def test_full_scan_over_the_limit_falls_back_to_footer_facts(tmp_path):
    path = write(tmp_path / "a.parquet", row_group_size=100, compression="none")
    r = profile(path, "full", max_full_bytes=4000)
    assert r["complete"] is False and "exceeds the server's limit" in r["notes"][0]
    assert col(r, "count")["range_from"] == "footer"


def test_columns_too_large_for_the_budget_are_reported_from_the_footer_alone(tmp_path):
    r = profile(write(tmp_path / "a.parquet", compression="none"), sample_bytes=10)
    count = col(r, "count")
    assert count["rows_read"] == 0 and (count["min"], count["max"]) == (0, 999) and "mean" not in count
    assert any("larger than the read budget" in n for n in r["notes"])


def test_files_without_footer_statistics_use_the_rows_read(tmp_path):
    path = write(tmp_path / "a.parquet", row_group_size=100, compression="none", write_statistics=False)
    count = col(profile(path, sample_bytes=4000), "count")
    assert count["range_from"] == "rows read" and "null_count" not in count


def test_columns_beyond_the_limit_are_listed_by_name(tmp_path):
    r = profile(write(tmp_path / "a.parquet"), max_columns=2)
    assert r["complete"] is False and r["columns"][2] == {"name": "longitude", "profiled": False}
    assert "first 2 of 9" in r["notes"][-1]


def geo_table(crs=None, bbox=(-150.0, 60.0, -140.0, 70.0)):
    column = {"encoding": "WKB", "geometry_types": ["Polygon"], "bbox": list(bbox)}
    if crs is not None:
        column["crs"] = crs
    geo = {"version": "1.0.0", "primary_column": "geometry", "columns": {"geometry": column}}
    table = pa.table({"geometry": pa.array([b"\x01\x02", b"\x03"], pa.binary()), "area_ha": pa.array([1.5, 2.5])})
    return table.replace_schema_metadata({"geo": json.dumps(geo), "source": "survey"})


def test_geoparquet_extent_comes_from_its_metadata_without_reading_geometries(tmp_path):
    r = profile(write(tmp_path / "g.parquet", geo_table()))
    assert r["format"] == "geoparquet" and r["complete"] is True
    geometry = r["geometry"]["columns"][0]
    assert geometry["geometry_types"] == ["Polygon"] and geometry["crs"]["epsg"] == 4326
    assert r["inferred_spatial_extent"]["bbox"] == {"north": 70.0, "south": 60.0, "east": -140.0, "west": -150.0}
    assert col(r, "geometry") == {
        "name": "geometry",
        "type": "geometry",
        "arrow_type": "binary",
        "non_null": 2,
        "null_count": 0,
    }
    assert col(r, "area_ha")["unit_guess"] == "ha"
    assert r["file_metadata"]["key_value"] == {"source": "survey"}
    assert any("Geometry columns were not read" in n for n in r["notes"])


def test_geoparquet_in_a_projected_crs_gets_a_latitude_longitude_box(tmp_path):
    crs = {"name": "NAD83 / Alaska Albers", "id": {"authority": "EPSG", "code": 3338}}
    r = profile(write(tmp_path / "g.parquet", geo_table(crs, (-200000, 1900000, -100000, 2000000))))
    geometry = r["geometry"]["columns"][0]
    assert geometry["crs"] == {"name": "NAD83 / Alaska Albers", "epsg": 3338, "geographic": False}
    assert geometry["bbox_native"]["west"] == -200000
    box = r["inferred_spatial_extent"]["bbox"]
    assert 60 < box["south"] < box["north"] < 75 and -165 < box["west"] < box["east"] < -150


def test_a_file_that_is_not_parquet_is_a_tool_error(tmp_path):
    path = tmp_path / "bad.parquet"
    path.write_bytes(b"not a parquet file at all")
    with pytest.raises(ToolError, match="Could not read the file as Parquet"):
        profile(str(path))


def test_remote_file_is_read_in_parts(tmp_path, mock_api):
    path = tmp_path / "a.parquet"
    rng = np.random.default_rng(0)
    table = pa.table({"a": pa.array(rng.random(200_000)), "b": pa.array(rng.random(200_000))})
    write(path, table, row_group_size=20_000, compression="none")
    route, size = serve(mock_api, path)
    r = profile(URL, size=size, sample_bytes=400_000)
    assert r["rows_total"] == 200_000 and r["complete"] is False
    assert r["read"]["file_size"] == size and r["read"]["bytes_fetched"] < size / 3
    assert r["read"]["requests"] == route.call_count < 10
    a = col(r, "a")
    assert a["range_from"] == "footer" and a["min"] == pytest.approx(table["a"].to_numpy().min())
    assert 0 < a["rows_read"] < 200_000


def test_remote_file_matches_the_local_profile_and_finds_its_own_size(tmp_path, mock_api):
    path = tmp_path / "a.parquet"
    write(path)
    serve(mock_api, path)
    remote = profile(URL, "full")
    assert remote.pop("read")["requests"] >= 1
    assert remote == profile(str(path), "full")


def test_a_small_remote_file_is_fetched_in_one_request(tmp_path, mock_api):
    path = tmp_path / "a.parquet"
    write(path)
    route, size = serve(mock_api, path)
    r = profile(URL, size=size)
    assert r["read"] == {"bytes_fetched": size, "requests": 1, "file_size": size} and route.call_count == 1


def test_a_server_without_range_support_is_a_tool_error(mock_api):
    mock_api.get(URL).mock(return_value=httpx.Response(200, content=b"x" * 100))
    with pytest.raises(ToolError, match="does not support range requests"):
        profile(URL, size=100)


def test_remote_read_gives_up_at_the_timeout(tmp_path, mock_api):
    path = tmp_path / "a.parquet"
    write(path)
    _, size = serve(mock_api, path)
    with pytest.raises(ToolError, match="took longer than"):
        profile(URL, size=size, timeout=-1.0)


async def test_profile_file_reads_parquet_over_range_requests(client, mock_api, tmp_path):
    path = tmp_path / "a.parquet"
    write(path)
    pid, quoted = "urn:uuid:pq", "urn%3Auuid%3Apq"
    route, size = serve(mock_api, path, f"{MN}/object/{quoted}")
    mock_api.get(f"{MN}/meta/{quoted}").mock(
        return_value=httpx.Response(
            200, text=SYSMETA.format(pid=pid, fmt="application/vnd.apache.parquet", size=size, name="a.parquet")
        )
    )
    sc = (await client.call_tool("profile_file", {"pid": pid, "scan": "full"})).structured_content
    assert sc["supported"] and sc["kind"] == "table" and sc["format"] == "parquet" and sc["complete"] is True
    assert sc["scan"] == "full" and sc["fileName"] == "a.parquet" and sc["rows_total"] == 1000
    assert route.called and all("range" in call.request.headers for call in route.calls)
