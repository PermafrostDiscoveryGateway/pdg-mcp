import netCDF4
import numpy as np
import pytest
from fastmcp.exceptions import ToolError

from arcticdata_mcp import profiling_netcdf
from arcticdata_mcp.config import ProfileSettings
from arcticdata_mcp.profiling_netcdf import slabs


def make_nc(path, fmt="NETCDF4", longitudes=None, names=("time", "lat", "lon"), attrs=True):
    t, la, lo = names
    with netCDF4.Dataset(path, "w", format=fmt) as ds:
        ds.createDimension(t, None)
        ds.createDimension(la, 4)
        ds.createDimension(lo, 5)
        time = ds.createVariable(t, "f8", (t,))
        lat = ds.createVariable(la, "f4", (la,))
        lon = ds.createVariable(lo, "f4", (lo,))
        temp = ds.createVariable("temp", "f4", (t, la, lo), fill_value=-999.0)
        time[:] = [0, 1, 2]
        lat[:] = [60, 61, 62, 63]
        lon[:] = longitudes if longitudes is not None else [-150, -149, -148, -147, -146]
        data = np.arange(60, dtype="f4").reshape(3, 4, 5)
        data[0, 0, 0] = -999
        temp[:] = data
        if attrs:
            time.units, time.calendar = "days since 2000-01-01", "standard"
            lat.units, lat.standard_name = "degrees_north", "latitude"
            lon.units, lon.standard_name = "degrees_east", "longitude"
            temp.units, temp.long_name = "K", "air temperature"
            ds.title, ds.Conventions, ds.history = "Test grid", "CF-1.8", "made in a test"
            ds.setncattr("internal_note", "not in the short list")
    return str(path)


def profile(path, scan="sample", **limits):
    return profiling_netcdf.profile_netcdf(str(path), ProfileSettings(**limits), scan)


def var(report, name):
    return next(v for v in report["variables"] if v["name"] == name)


def test_dimensions_attributes_and_variables(tmp_path):
    r = profile(make_nc(tmp_path / "a.nc"))
    assert r["kind"] == "netcdf" and r["file_format"] == "NETCDF4"
    assert r["dimensions"] == [
        {"name": "time", "size": 3, "unlimited": True}, {"name": "lat", "size": 4, "unlimited": False},
        {"name": "lon", "size": 5, "unlimited": False},
    ]
    assert r["global_attributes"] == {"title": "Test grid", "Conventions": "CF-1.8", "history": "made in a test"}
    assert r["other_global_attributes"] == 1
    temp = var(r, "temp")
    assert (temp["units"], temp["long_name"], temp["dtype"], temp["shape"]) == ("K", "air temperature", "float32", [3, 4, 5])
    assert temp["dimensions"] == ["time", "lat", "lon"] and temp["_FillValue"] == pytest.approx(-999.0)


def test_fill_values_are_excluded_from_statistics(tmp_path):
    stats = var(profile(make_nc(tmp_path / "a.nc")), "temp")["stats"]
    assert (stats["min"], stats["max"], stats["valid_count"], stats["basis"]) == (1.0, 59.0, 59, "all values")


def test_extents_come_from_the_coordinate_variables(tmp_path):
    r = profile(make_nc(tmp_path / "a.nc"))
    assert r["spatial_extent"]["bbox"] == {"north": 63.0, "south": 60.0, "east": -146.0, "west": -150.0}
    assert (r["spatial_extent"]["latitude_variable"], r["spatial_extent"]["longitude_variable"]) == ("lat", "lon")
    assert r["temporal_extent"]["start"].startswith("2000-01-01") and r["temporal_extent"]["end"].startswith("2000-01-03")
    assert var(r, "lat")["role"] == "latitude" and var(r, "time")["role"] == "time"
    assert r["complete"] is True


def test_classic_format_works_too(tmp_path):
    r = profile(make_nc(tmp_path / "c.nc", fmt="NETCDF3_CLASSIC"))
    assert r["file_format"] == "NETCDF3_CLASSIC" and r["spatial_extent"] is not None


def test_coordinates_are_found_by_name_without_attributes(tmp_path):
    r = profile(make_nc(tmp_path / "n.nc", attrs=False, names=("t", "latitude", "lon")))
    assert r["spatial_extent"]["latitude_variable"] == "latitude" and r["temporal_extent"] is None


def test_zero_to_360_longitudes_are_flagged(tmp_path):
    r = profile(make_nc(tmp_path / "w.nc", longitudes=[200, 210, 220, 230, 240]))
    assert r["spatial_extent"]["longitude_convention"] == "0-360" and r["spatial_extent"]["bbox"]["west"] == 200.0


def test_implausible_latitudes_give_no_extent(tmp_path):
    path = tmp_path / "bad.nc"
    with netCDF4.Dataset(path, "w") as ds:
        ds.createDimension("n", 3)
        for name, values in (("lat", [100, 200, 300]), ("lon", [1, 2, 3])):
            ds.createVariable(name, "f4", ("n",))[:] = values
    assert profile(path)["spatial_extent"] is None


def test_large_variables_are_read_from_both_ends(tmp_path):
    path = tmp_path / "big.nc"
    with netCDF4.Dataset(path, "w") as ds:
        ds.createDimension("x", 1000)
        ds.createDimension("y", 100)
        ds.createVariable("v", "f8", ("x", "y"))[:] = np.arange(100_000, dtype="f8").reshape(1000, 100)
    r = profile(path, sample_values=500)
    stats = var(r, "v")["stats"]
    assert stats["basis"] == "start and end of the variable" and stats["valid_count"] == 400
    assert (stats["min"], stats["max"]) == (0.0, 99_999.0)  # the first and last rows hold the extremes
    assert r["complete"] is False and any("scan='full'" in n for n in r["notes"])
    full = profile(path, "full", sample_values=500)
    assert full["complete"] is True and var(full, "v")["stats"]["valid_count"] == 100_000


def test_full_scan_budget_is_spent_on_small_variables_first(tmp_path):
    path = tmp_path / "two.nc"
    with netCDF4.Dataset(path, "w") as ds:
        ds.createDimension("n", 10_000)
        ds.createVariable("small", "f8", ("n",))[:] = np.arange(10_000)
        ds.createVariable("also_small", "f8", ("n",))[:] = np.arange(10_000)
        ds.createDimension("m", 100_000)
        ds.createVariable("large", "f8", ("m",))[:] = np.arange(100_000)
    r = profile(path, "full", sample_values=100, max_full_values=25_000)
    assert var(r, "small")["stats"]["basis"] == "all values" and var(r, "also_small")["stats"]["basis"] == "all values"
    assert var(r, "large")["stats"]["basis"] == "start and end of the variable" and r["complete"] is False


def test_groups_strings_and_scalars(tmp_path):
    path = tmp_path / "g.nc"
    with netCDF4.Dataset(path, "w") as ds:
        ds.createDimension("n", 3)
        ds.createVariable("scalar", "f8", ())[...] = 7.5
        ds.createVariable("label", str, ("n",))[:] = np.array(["a", "b", "c"], dtype=object)
        group = ds.createGroup("model")
        group.createDimension("k", 2)
        group.createVariable("x", "i4", ("k",))[:] = [3, 4]
    r = profile(path)
    assert var(r, "scalar")["stats"]["min"] == 7.5 and var(r, "scalar")["shape"] == []
    assert var(r, "label")["dtype"] == "string" and "stats" not in var(r, "label")
    assert var(r, "model/x")["stats"]["max"] == 4.0 and {"name": "model/k", "size": 2, "unlimited": False} in r["dimensions"]


def test_variable_cap_lists_the_rest(tmp_path):
    path = tmp_path / "many.nc"
    with netCDF4.Dataset(path, "w") as ds:
        ds.createDimension("n", 2)
        for i in range(5):
            ds.createVariable(f"v{i}", "f4", ("n",))[:] = [1, 2]
    r = profile(path, max_columns=2)
    assert len(r["variables"]) == 5 and r["variables"][4] == {"name": "v4", "profiled": False}
    assert r["complete"] is False and any("first 2 of 5" in n for n in r["notes"])


def test_a_non_netcdf_file_is_rejected(tmp_path):
    path = tmp_path / "fake.nc"
    path.write_text("a,b\n1,2\n")
    with pytest.raises(ToolError, match="NetCDF"):
        profile(path)


def test_slabs_read_everything_when_it_fits():
    assert slabs((10, 10), 100) == ([(Ellipsis,)], True)


def test_slabs_take_whole_trailing_dimensions():
    parts, whole = slabs((1000, 100), 500)
    assert whole is False and parts == [(slice(0, 2), slice(None)), (slice(-2, None), slice(None))]


def test_slabs_pin_leading_dimensions_to_one_index():
    parts, _ = slabs((10, 1000, 1000), 100)
    assert parts[0] == (0, 0, slice(0, 50)) and parts[1] == (-1, -1, slice(-50, None))


@pytest.mark.parametrize(
    "fmt,name,expected",
    [("netCDF-4", "a.nc", True), ("netCDF-3", "a.cdf", True), ("application/octet-stream", "a.nc4", True),
     ("application/octet-stream", "a.bin", False), ("text/csv", "a.csv", False), ("application/x-hdf5", "a.h5", False)],
)
def test_is_netcdf(fmt, name, expected):
    assert profiling_netcdf.is_netcdf(fmt, name) is expected


async def test_subprocess_profile_matches_the_in_process_one(tmp_path):
    path = make_nc(tmp_path / "a.nc")
    report = await profiling_netcdf.profile_in_subprocess(str(path), ProfileSettings(), "sample", 60)
    assert report["kind"] == "netcdf" and report["spatial_extent"] == profile(path)["spatial_extent"]


async def test_subprocess_profile_reports_unreadable_files_as_tool_errors(tmp_path):
    bad = tmp_path / "bad.nc"
    bad.write_bytes(b"not netcdf")
    with pytest.raises(ToolError, match="Could not read"):
        await profiling_netcdf.profile_in_subprocess(str(bad), ProfileSettings(), "sample", 60)


async def test_subprocess_profile_is_killed_at_the_timeout(tmp_path):
    path = make_nc(tmp_path / "a.nc")
    with pytest.raises(ToolError, match="longer than"):
        await profiling_netcdf.profile_in_subprocess(str(path), ProfileSettings(), "sample", 0.01)


def test_a_variable_named_long_but_not_in_degrees_is_not_a_longitude(tmp_path):
    path = tmp_path / "flux.nc"
    with netCDF4.Dataset(path, "w") as ds:
        ds.createDimension("t", 3)
        lat = ds.createVariable("lat", "f8", ())
        lat.units = "deg"
        lat[:] = 66.3
        flux = ds.createVariable("down_long_hemisp", "f8", ("t",))
        flux.units = "W/m2"
        flux[:] = [200.0, 250.0, 300.0]
    r = profile(path)
    assert r["spatial_extent"] is None
    assert var(r, "lat")["role"] == "latitude" and var(r, "down_long_hemisp").get("role") is None
