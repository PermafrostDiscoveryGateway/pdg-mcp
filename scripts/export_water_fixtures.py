"""Export water-timeseries-v2 test fixtures into the small JSON files the water tools serve.

The MCP server reads plain JSON so it does not depend on geopandas/xarray. Re-run this when the
source fixtures change:

    uv run --no-project --python 3.12 --with geopandas --with pyarrow --with "xarray<2026" --with netcdf4 \
        python scripts/export_water_fixtures.py ../water-timeseries-v2/tests/data src/arcticdata_mcp/data/water
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely
import xarray as xr

# Lake attributes carried into the GeoJSON (source column -> output property).
PROPERTIES = {
    "id_geohash": "id_geohash",
    "Area_start_ha": "area_start_ha",
    "Area_end_ha": "area_end_ha",
    "NetChange_ha": "net_change_ha",
    "NetChange_perc": "net_change_perc",
}
SIMPLIFY_DEG = 0.0001  # ~10 m at these latitudes; plenty for an overview map
PRECISION_DEG = 0.00001


def _num(v: float) -> float | None:
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), 3)


def export_lakes(src: Path, with_timeseries: set[str]) -> dict:
    files = ("lake_polygons.parquet", "lakes_polygons_test.parquet")
    lakes = pd.concat([gpd.read_parquet(src / f).to_crs("EPSG:4326") for f in files]).drop_duplicates("id_geohash")
    geoms = shapely.set_precision(lakes.geometry.simplify(SIMPLIFY_DEG, preserve_topology=True).values, PRECISION_DEG)
    features = []
    for (_, row), geom in zip(lakes.iterrows(), geoms, strict=True):
        props = {out: (row[col] if col == "id_geohash" else _num(row[col])) for col, out in PROPERTIES.items()}
        props["has_timeseries"] = row["id_geohash"] in with_timeseries
        features.append({"type": "Feature", "properties": props, "geometry": shapely.geometry.mapping(geom)})
    return {"type": "FeatureCollection", "features": features}


def export_timeseries(src: Path) -> dict:
    ds = xr.open_dataset(src / "lakes_dw_test.nc")
    out = {
        "source": "Dynamic World V1 monthly class areas (water-timeseries-v2 test fixture)",
        "unit": "ha",
        "dates": [str(d)[:10] for d in ds["date"].values],
        "lakes": {},
    }
    for lake in ds["id_geohash"].values:
        sel = ds.sel(id_geohash=lake)
        out["lakes"][str(lake)] = {str(v): [_num(x) for x in sel[v].values] for v in ds.data_vars}
    return out


def main(src: str, dest: str) -> None:
    src_dir, dest_dir = Path(src), Path(dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    timeseries = export_timeseries(src_dir)
    lakes = export_lakes(src_dir, set(timeseries["lakes"]))
    (dest_dir / "timeseries.json").write_text(json.dumps(timeseries, separators=(",", ":")))
    (dest_dir / "lakes.geojson").write_text(json.dumps(lakes, separators=(",", ":")))
    print(f"{len(lakes['features'])} lakes, {len(timeseries['lakes'])} time series -> {dest_dir}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
