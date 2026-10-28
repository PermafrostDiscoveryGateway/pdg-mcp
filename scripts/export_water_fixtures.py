"""Export water-timeseries-v2 test fixtures into the small JSON files the water tools serve.

All processing goes through the water_timeseries library, so the data matches what its dashboard
shows: DWDataset/JRCDataset masking and normalization and SimpleBreakpoint break detection (the
dashboard's "Historical" mode uses break_method=simple). The series are exported as plain data; the
time series view (ui/lake_timeseries.html) draws the dashboard's charts from them. The MCP server only
reads plain JSON, so it does not depend on the library or its geo/Earth Engine stack.

The library needs its full environment; water-timeseries-v2's lockfile currently pins numpy<2 with
a pyarrow that needs numpy 2, so run it in an isolated env with an older pyarrow:

    uv run --no-project --python 3.12 --with ../water-timeseries-v2 --with "pyarrow<18" \
        python scripts/export_water_fixtures.py ../water-timeseries-v2/tests/data src/arcticdata_mcp/data/water
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely
import xarray as xr
from water_timeseries.breakpoint import SimpleBreakpoint
from water_timeseries.dataset import DWDataset, JRCDataset
from water_timeseries.map_utils import geohash_to_human_readable_name
from water_timeseries.utils.plotting import prepare_data_for_plot_dw

# Lake attributes carried into the GeoJSON, under the dashboard's column names.
LAKE_COLUMNS = ["id_geohash", "Area_start_ha", "Area_end_ha", "NetChange_ha", "NetChange_perc"]
BREAK_COLUMNS = [
    "date_break_year",
    "date_break_month",
    "pre_break_median",
    "post_break_median",
    "water_change_ha",
    "water_change_perc",
]
SIMPLIFY_DEG = 0.0001  # ~10 m at these latitudes; plenty for an overview map
PRECISION_DEG = 0.00001


def _num(v) -> float | None:
    if v is None or pd.isna(v):
        return None
    return round(float(v), 3)


def _date(v) -> str | None:
    return None if v is None or pd.isna(v) else pd.Timestamp(v).strftime("%Y-%m-%d")


def _series(df: pd.DataFrame) -> dict:
    """Long (date, variable, value) frame -> {"dates": [...], variable: [...]} on a shared date axis."""
    wide = df.pivot_table(index="date", columns="variable", values="value", aggfunc="first").sort_index()
    out: dict = {"dates": [_date(d) for d in wide.index]}
    for col in wide.columns:
        # 6 decimals (about 1 mm2): the chart's relative-area hover text divides these, so 3 would show.
        out[col] = [None if pd.isna(v) else round(float(v), 6) for v in wide[col]]
    return out


def export_timeseries(src: Path) -> tuple[dict, pd.DataFrame]:
    dw = DWDataset(xr.open_dataset(src / "lakes_dw_test.nc"))
    jrc = JRCDataset(xr.open_dataset(src / "lakes_jrc_test.nc"))
    breaks = SimpleBreakpoint().calculate_breaks_batch(dw)

    lakes = {}
    for lake in dw.object_ids_:
        brk = breaks.loc[lake] if lake in breaks.index else None
        # The dashboard titles the time series with this name ("<geohash> | lat : lon").
        entry: dict = {"name": geohash_to_human_readable_name(lake), "break": None}
        if brk is not None:
            entry["break"] = {
                "date_break": _date(brk["date_break"]),
                "date_before_break": _date(brk["date_before_break"]),
                "date_after_break": _date(brk["date_after_break"]),
                "break_method": brk["break_method"],
                **{c: _num(brk[c]) for c in BREAK_COLUMNS},
            }

        # Same masked frame and vegetation grouping DWDataset.plot_timeseries_interactive uses.
        df = dw.ds.sel(id_geohash=lake).load().to_dataframe().dropna()
        entry["dw"] = {
            "source": "Dynamic World V1, monthly (June-September)",
            "unit": "ha",
            "normalization_factor": round(float(df["area_data"].max()), 6),
            "data": _series(prepare_data_for_plot_dw(df, group_vegetation=True)),
        }
        if lake in jrc.object_ids_:
            jdf = jrc.ds.sel(id_geohash=lake).load().to_dataframe().dropna()
            jlong = jdf.reset_index().melt(
                id_vars="date",
                value_vars=["area_water_permanent", "area_water_seasonal", "area_land"],
                var_name="variable",
            )
            entry["jrc"] = {
                "source": "JRC Global Surface Water, annual",
                "unit": "ha",
                "data": _series(jlong),
            }
        lakes[lake] = entry
    return {"lakes": lakes}, breaks


def export_lakes(src: Path, timeseries: dict, breaks: pd.DataFrame) -> dict:
    files = ("lake_polygons.parquet", "lakes_polygons_test.parquet")
    lakes = pd.concat([gpd.read_parquet(src / f).to_crs("EPSG:4326") for f in files]).drop_duplicates("id_geohash")
    geoms = shapely.set_precision(lakes.geometry.simplify(SIMPLIFY_DEG, preserve_topology=True).values, PRECISION_DEG)
    features = []
    for (_, row), geom in zip(lakes.iterrows(), geoms, strict=True):
        lake = row["id_geohash"]
        props = {c: (lake if c == "id_geohash" else _num(row[c])) for c in LAKE_COLUMNS}
        props["has_timeseries"] = lake in timeseries["lakes"]
        if lake in breaks.index:
            brk = breaks.loc[lake]
            props["date_break"] = pd.Timestamp(brk["date_break"]).strftime("%Y-%m")
            props.update({c: _num(brk[c]) for c in BREAK_COLUMNS})
            props["date_break_year"] = int(brk["date_break_year"])
            props["date_break_month"] = int(brk["date_break_month"])
        features.append({"type": "Feature", "properties": props, "geometry": shapely.geometry.mapping(geom)})
    return {"type": "FeatureCollection", "features": features}


def main(src: str, dest: str) -> None:
    src_dir, dest_dir = Path(src), Path(dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    timeseries, breaks = export_timeseries(src_dir)
    lakes = export_lakes(src_dir, timeseries, breaks)
    (dest_dir / "timeseries.json").write_text(json.dumps(timeseries, separators=(",", ":")))
    (dest_dir / "lakes.geojson").write_text(json.dumps(lakes, separators=(",", ":")))
    drained = sum(1 for f in lakes["features"] if "date_break" in f["properties"])
    print(f"{len(lakes['features'])} lakes, {len(timeseries['lakes'])} time series, {drained} breaks -> {dest_dir}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
