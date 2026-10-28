"""Build the lakes.parquet store that the server queries with DuckDB (see arcticdata_mcp/waterdata.py).

Reads the water-timeseries-v2 lake layer (or the sample from scripts/make_water_sample.py) and writes one row per
lake with what show_lakes needs, so the server never decodes geometry or scans the whole file:

- the attributes show_lakes returns (areas, break columns, date_break as "YYYY-MM", has_timeseries),
- west/south/east/north and lon/lat (centroid) for bounding-box queries and zoomed-out dots,
- geom: simplified GeoJSON geometry (same simplification and precision as export_water_fixtures.py),
- rows sorted by id_geohash (a geohash prefix is a map tile) in small row groups, so a bbox query reads only the
  row groups that overlap it.

timeseries.json is copied next to it (only the lakes in it get has_timeseries=true). Needs the ingest group:

    uv run --group ingest python scripts/build_water_store.py \
        data/water-sample/lakes_sample.parquet data/water-sample

    ARCTICDATA_MCP_WATER_DATA_DIR=data/water-sample uv run arcticdata-mcp --transport http --insecure
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import shapely

BUNDLED = Path(__file__).resolve().parent.parent / "src" / "arcticdata_mcp" / "data" / "water"
SOURCE_COLUMNS = [
    "id_geohash", "Area_start_ha", "Area_end_ha", "NetChange_ha", "NetChange_perc",
    "date_break", "pre_break_median", "post_break_median", "water_change_ha", "water_change_perc",
    "date_break_year", "date_break_month", "geometry",
]  # fmt: skip
NUMERIC = [
    "Area_start_ha", "Area_end_ha", "NetChange_ha", "NetChange_perc",
    "pre_break_median", "post_break_median", "water_change_ha", "water_change_perc",
]  # fmt: skip
SIMPLIFY_DEG = 0.0001
PRECISION_DEG = 0.00001
ROW_GROUP_SIZE = 20_000
BATCH_ROWS = 50_000


def _geojson(geoms: np.ndarray) -> list[str | None]:
    return [None if g is None else shapely.to_geojson(g) for g in geoms]


def convert(batch: pa.RecordBatch, with_series: set[str]) -> pa.Table:
    df = batch.to_pandas()
    geoms = shapely.from_wkb(df["geometry"].to_numpy())
    geoms = shapely.set_precision(shapely.simplify(geoms, SIMPLIFY_DEG, preserve_topology=True), PRECISION_DEG)
    west, south, east, north = shapely.bounds(geoms).T
    centers = shapely.centroid(geoms)
    out = {"id_geohash": df["id_geohash"]}
    for c in NUMERIC:
        out[c] = df[c].round(3)
    out["has_timeseries"] = df["id_geohash"].isin(with_series)
    out["date_break"] = df["date_break"].dt.strftime("%Y-%m")
    out["date_break_year"] = df["date_break_year"].astype("Int16")
    out["date_break_month"] = df["date_break_month"].astype("Int8")
    out.update(
        west=west, south=south, east=east, north=north, lon=shapely.get_x(centers), lat=shapely.get_y(centers),
        geom=_geojson(geoms),
    )  # fmt: skip
    return pa.Table.from_pandas(pd.DataFrame(out), preserve_index=False)


def main(src: str, dest: str) -> None:
    out = Path(dest)
    out.mkdir(parents=True, exist_ok=True)
    with_series = set(json.loads((BUNDLED / "timeseries.json").read_text())["lakes"])
    pf = pq.ParquetFile(src)
    with tempfile.TemporaryDirectory(dir=out) as tmp:
        unsorted = Path(tmp) / "unsorted.parquet"
        writer = None
        n = 0
        for batch in pf.iter_batches(batch_size=BATCH_ROWS, columns=SOURCE_COLUMNS):
            table = convert(batch, with_series)
            writer = writer or pq.ParquetWriter(unsorted, table.schema, compression="zstd")
            writer.write_table(table)
            n += table.num_rows
            print(f"\r{n} lakes", end="", flush=True)
        writer.close()
        print()
        # DuckDB sorts out of core, so the full layer never has to fit in memory.
        con = duckdb.connect()
        con.execute(f"SET temp_directory = '{tmp}'")
        con.execute(
            f"COPY (SELECT * FROM read_parquet('{unsorted}') ORDER BY id_geohash) TO '{out / 'lakes.parquet'}' "
            f"(FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {ROW_GROUP_SIZE})"
        )
    shutil.copy(BUNDLED / "timeseries.json", out / "timeseries.json")
    print(f"{n} lakes -> {out / 'lakes.parquet'}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
