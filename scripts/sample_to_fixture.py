"""Turn the GeoParquet sample (scripts/make_water_sample.py) into the files WaterData reads today.

Writes lakes.geojson with the same properties scripts/export_water_fixtures.py produces, and copies the
bundled timeseries.json (only the 5 test lakes have a series), so the unmodified server can run on the sample:

    ../water-timeseries-v2/.venv/bin/python scripts/sample_to_fixture.py \
        data/water-sample/lakes_sample.parquet data/water-sample

    ARCTICDATA_MCP_WATER_DATA_DIR=data/water-sample uv run arcticdata-mcp --transport http --insecure
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely

BUNDLED = Path(__file__).resolve().parent.parent / "src" / "arcticdata_mcp" / "data" / "water"
LAKE_COLUMNS = ["Area_start_ha", "Area_end_ha", "NetChange_ha", "NetChange_perc"]
BREAK_COLUMNS = ["pre_break_median", "post_break_median", "water_change_ha", "water_change_perc"]
SIMPLIFY_DEG = 0.0001  # same as export_water_fixtures.py
PRECISION_DEG = 0.00001


def _num(v) -> float | None:
    return None if v is None or pd.isna(v) else round(float(v), 3)


def main(src: str, dest: str) -> None:
    lakes = gpd.read_parquet(src).to_crs("EPSG:4326")
    with_series = set(json.loads((BUNDLED / "timeseries.json").read_text())["lakes"])
    geoms = shapely.set_precision(lakes.geometry.simplify(SIMPLIFY_DEG, preserve_topology=True).values, PRECISION_DEG)
    features = []
    for (_, row), geom in zip(lakes.iterrows(), geoms, strict=True):
        props = {"id_geohash": row["id_geohash"], **{c: _num(row[c]) for c in LAKE_COLUMNS}}
        props["has_timeseries"] = row["id_geohash"] in with_series
        if pd.notna(row["date_break"]):
            props["date_break"] = pd.Timestamp(row["date_break"]).strftime("%Y-%m")
            props.update({c: _num(row[c]) for c in BREAK_COLUMNS})
            props["date_break_year"] = int(row["date_break_year"])
            props["date_break_month"] = int(row["date_break_month"])
        features.append({"type": "Feature", "properties": props, "geometry": shapely.geometry.mapping(geom)})
    out = Path(dest)
    out.mkdir(parents=True, exist_ok=True)
    (out / "lakes.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":")))
    shutil.copy(BUNDLED / "timeseries.json", out / "timeseries.json")
    print(f"{len(features)} lakes -> {out}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
