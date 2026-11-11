"""Cut a small GeoParquet sample out of the full water-timeseries-v2 lake layer.

The full file (DW_historicalbp_simple_merged_breaks_with_allgeoms_v4.parquet) holds ~4M lake polygons with
their attributes and drainage-break columns, about 3 GB. This keeps the same schema and a subset, so the
spatial store, paging and zoom-dependent simplification can be developed without it:

- every lake in the Seward Peninsula + Kotzebue Sound box (~25k, dense, includes the bundled fixture area),
- every lake with a detected drainage break anywhere (~10k, so the drainage coloring and zooming out to the
  whole Arctic still have data; pass "box-only" to leave these out),
- the lakes that have a test time series (lakes_dw_test.nc / lakes_jrc_test.nc).

    ../water-timeseries-v2/.venv/bin/python scripts/make_water_sample.py \
        ../water-timeseries-v2/data/DW_historicalbp_simple_merged_breaks_with_allgeoms_v4.parquet \
        ../water-timeseries-v2/tests/data data/water-sample/lakes_sample.parquet [box-only]
"""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyarrow.parquet as pq
import xarray as xr

# Seward Peninsula + Kotzebue Sound: west, south, east, north.
SAMPLE_BBOX = (-168.5, 63.5, -156.0, 68.5)


def test_series_ids(test_data: Path) -> set[str]:
    ids: set[str] = set()
    for name in ("lakes_dw_test.nc", "lakes_jrc_test.nc"):
        with xr.open_dataset(test_data / name) as ds:
            ids |= set(ds.id_geohash.values.tolist())
    return ids


def main(src: str, test_data: str, dest: str, mode: str = "") -> None:
    pf = pq.ParquetFile(src)
    keep_ids = test_series_ids(Path(test_data))
    parts = []
    # One row group at a time so the full file never sits in memory.
    for i in range(pf.metadata.num_row_groups):
        gdf = gpd.GeoDataFrame.from_arrow(pf.read_row_group(i))
        pick = gdf["id_geohash"].isin(keep_ids)
        if mode != "box-only":
            pick |= gdf["date_break"].notna()
        # bbox test on centroids in the file's own CRS, via a reprojected copy of the points only
        cen = gdf.geometry.to_crs(3857).centroid.to_crs(4326)
        w, s, e, n = SAMPLE_BBOX
        pick |= (cen.x >= w) & (cen.x <= e) & (cen.y >= s) & (cen.y <= n)
        parts.append(gdf[pick])
        print(f"row group {i}: kept {int(pick.sum())} of {len(gdf)}")
    out = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs)
    out = out.drop_duplicates("id_geohash")
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(dest, compression="zstd", index=False)
    missing = keep_ids - set(out["id_geohash"])
    print(f"{len(out)} lakes ({out['date_break'].notna().sum()} drained) -> {dest}; missing test lakes: {missing or 'none'}")


if __name__ == "__main__":
    main(*sys.argv[1:5])
