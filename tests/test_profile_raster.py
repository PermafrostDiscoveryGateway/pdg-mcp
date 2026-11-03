import numpy as np
import pytest
import rasterio
from fastmcp.exceptions import ToolError
from rasterio.transform import from_origin

from arcticdata_mcp import profiling_raster
from arcticdata_mcp.config import ProfileSettings


def make_tif(path, data, crs="EPSG:4326", transform=from_origin(-150, 70, 0.1, 0.1), nodata=None, **options):
    data = np.asarray(data)
    if data.ndim == 2:
        data = data[np.newaxis]
    with rasterio.open(
        path, "w", driver="GTiff", count=data.shape[0], height=data.shape[1], width=data.shape[2],
        dtype=data.dtype, crs=crs, transform=transform, nodata=nodata, **options,
    ) as dst:
        dst.write(data)
        return dst.name


def profile(path, scan="sample", **limits):
    return profiling_raster.profile_raster(str(path), ProfileSettings(**limits), scan)


def test_georeferencing_and_band_statistics(tmp_path):
    data = np.arange(80 * 100, dtype="float32").reshape(80, 100)
    data[0, :10] = -9999
    r = profile(make_tif(tmp_path / "a.tif", data, nodata=-9999))
    assert (r["width"], r["height"], r["band_count"]) == (100, 80, 1)
    assert r["crs"]["epsg"] == 4326 and r["crs"]["geographic"] is True
    assert r["pixel_size"] == {"x": 0.1, "y": 0.1}
    assert r["spatial_extent"]["bbox"] == pytest.approx({"north": 70, "south": 62, "east": -140, "west": -150})
    band = r["bands"][0]
    assert (band["dtype"], band["nodata"], band["min"], band["max"]) == ("float32", -9999.0, 10.0, 7999.0)
    assert band["valid_fraction"] == pytest.approx(1 - 10 / 8000, abs=1e-4)
    assert band["mean"] == pytest.approx(data[data != -9999].mean(), rel=1e-5)
    assert r["complete"] is True and "notes" in r and not r["notes"]


def test_nan_is_treated_as_missing(tmp_path):
    data = np.ones((20, 20), dtype="float32")
    data[:10] = np.nan
    band = profile(make_tif(tmp_path / "n.tif", data))["bands"][0]
    assert band["min"] == band["max"] == 1.0 and band["valid_fraction"] == 0.5


def test_projected_crs_gets_a_latitude_longitude_box(tmp_path):
    transform = from_origin(-200000, 2000000, 1000, 1000)  # Alaska Albers (EPSG:3338)
    r = profile(make_tif(tmp_path / "p.tif", np.ones((50, 60), "uint8"), crs="EPSG:3338", transform=transform))
    assert r["crs"]["epsg"] == 3338 and r["crs"]["geographic"] is False and r["crs"]["linear_units"]
    bbox = r["spatial_extent"]["bbox"]
    assert 55 < bbox["south"] < bbox["north"] < 75 and -180 < bbox["west"] < bbox["east"] < -120
    assert r["bounds_native"]["left"] == -200000


@pytest.mark.filterwarnings("ignore::rasterio.errors.NotGeoreferencedWarning")
def test_custom_crs_without_an_epsg_code_shows_its_definition(tmp_path):
    crs = rasterio.crs.CRS.from_proj4("+proj=aea +lat_1=50 +lat_2=70 +lat_0=60 +lon_0=-150 +datum=WGS84 +units=m")
    r = profile(make_tif(tmp_path / "k.tif", np.ones((10, 10), "uint8"), crs=crs, transform=from_origin(-100000, 100000, 1000, 1000)))
    assert r["crs"]["epsg"] is None and "+proj=aea" in r["crs"]["proj"] and r["spatial_extent"] is not None


@pytest.mark.filterwarnings("ignore::rasterio.errors.NotGeoreferencedWarning")
def test_missing_crs_is_reported(tmp_path):
    r = profile(make_tif(tmp_path / "x.tif", np.ones((10, 10), "uint8"), crs=None, transform=rasterio.Affine.identity()))
    assert r["crs"] is None and r["spatial_extent"] is None and any("coordinate reference system" in n for n in r["notes"])


def test_classified_raster_lists_its_values(tmp_path):
    data = np.random.default_rng(1).choice([1, 2, 5, 0], size=(40, 40)).astype("uint8")
    band = profile(make_tif(tmp_path / "c.tif", data, nodata=0))["bands"][0]
    assert band["distinct_values"] == [1.0, 2.0, 5.0] and band["nodata"] == 0.0


def test_many_distinct_values_are_not_listed(tmp_path):
    band = profile(make_tif(tmp_path / "d.tif", np.arange(900, dtype="uint16").reshape(30, 30)))["bands"][0]
    assert "distinct_values" not in band


def test_band_descriptions_units_and_scale(tmp_path):
    path = tmp_path / "m.tif"
    make_tif(path, np.ones((2, 16, 16), "int16"))
    with rasterio.open(path, "r+") as dst:
        dst.set_band_description(1, "temperature")
        dst.set_band_unit(1, "kelvin")
        dst.scales = (0.1, 1.0)
        dst.offsets = (273.15, 0.0)
    r = profile(path)
    one, two = r["bands"]
    assert (one["description"], one["unit"], one["scale"], one["offset"]) == ("temperature", "kelvin", 0.1, 273.15)
    assert two["description"] is None and "scale" not in two


def test_tiled_raster_is_sampled_unless_full(tmp_path):
    data = np.arange(1024 * 1024, dtype="float32").reshape(1024, 1024)
    path = make_tif(tmp_path / "t.tif", data, tiled=True, blockxsize=256, blockysize=256)
    sampled = profile(path, sample_blocks=2)
    assert sampled["complete"] is False and sampled["stats_basis"] == {"windows_read": 2, "windows_total": 16}
    assert sampled["layout"]["tiled"] is True and sampled["layout"]["block_size"] == [256, 256]
    assert any("2 of 16 blocks" in n for n in sampled["notes"])
    full = profile(path, "full")
    assert full["complete"] is True and full["stats_basis"]["windows_read"] == 16
    assert (full["bands"][0]["min"], full["bands"][0]["max"]) == (0.0, float(data.max()))


def test_full_scan_falls_back_to_sampling_for_huge_rasters(tmp_path):
    path = make_tif(tmp_path / "h.tif", np.ones((512, 512), "float32"), tiled=True, blockxsize=64, blockysize=64)
    r = profile(path, "full", max_full_pixels=100_000, sample_blocks=4)
    assert r["complete"] is False and any("sampled instead of read in full" in n for n in r["notes"])


def test_blocks_too_large_to_sample_skip_statistics(tmp_path):
    r = profile(make_tif(tmp_path / "b.tif", np.ones((64, 64), "float32"), tiled=True, blockxsize=64, blockysize=64), max_full_pixels=1000)
    assert "min" not in r["bands"][0] and any("too large" in n for n in r["notes"]) and r["complete"] is False


def test_strip_rasters_are_sampled_in_groups_of_rows(tmp_path):
    data = np.arange(4000 * 500, dtype="float32").reshape(4000, 500)
    r = profile(make_tif(tmp_path / "s.tif", data, blockysize=1), sample_blocks=5, sample_block_pixels=2000)
    assert r["stats_basis"]["windows_read"] == 5 and r["layout"]["block_size"] == [500, 1]
    assert r["bands"][0]["min"] == 0.0 and r["bands"][0]["max"] > 1_900_000  # the last window is near the bottom


def test_tags_are_reported(tmp_path):
    path = tmp_path / "g.tif"
    make_tif(path, np.ones((8, 8), "uint8"))
    with rasterio.open(path, "r+") as dst:
        dst.update_tags(DATASET_NAME="biomass", ACQUIRED="2020-07-01")
    tags = profile(path)["tags"]
    assert tags["DATASET_NAME"] == "biomass" and tags["ACQUIRED"] == "2020-07-01"


def test_a_non_raster_is_rejected(tmp_path):
    path = tmp_path / "fake.tif"
    path.write_text("a,b\n1,2\n")
    with pytest.raises(ToolError, match="raster"):
        profile(path)


@pytest.mark.parametrize(
    "fmt,name,expected",
    [("image/tiff", "a.tif", True), ("image/geotiff", "a", True), ("application/octet-stream", "a.TIF", True),
     ("application/octet-stream", "a.bin", False), ("image/png", "a.png", False), ("text/csv", "a.csv", False)],
)
def test_is_raster(fmt, name, expected):
    assert profiling_raster.is_raster(fmt, name) is expected
