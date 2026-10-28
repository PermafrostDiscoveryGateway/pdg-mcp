"""Lake data behind show_lakes and get_lake_timeseries.

``open_water_data(data_dir)`` picks the backend from what the directory holds:

* ``lakes.parquet`` (scripts/build_water_store.py): :class:`ParquetWaterData`, queried with DuckDB, so a bounding
  box reads only the row groups that overlap it and the full layer is never loaded into memory.
* ``lakes.geojson`` (scripts/export_water_fixtures.py), or the small fixture bundled with the package when
  ``data_dir`` is empty: :class:`JsonWaterData`, which filters in Python.

Both return the same :class:`LakeResult`. Lakes are ranked drained-first (biggest water change first), then
stable lakes by size, so a capped result keeps the lakes that matter. Geometry is chosen by the map zoom (given,
or implied by the bounding box): centroids when zoomed out, polygons when zoomed in, each with its own cap.
"""

from __future__ import annotations

import json
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import cached_property
from importlib.resources import files
from pathlib import Path
from typing import Any

BBox = tuple[float, float, float, float]  # west, south, east, north

# lake_map.html draws centroids below this zoom and polygons from here up (POINT_POLY_SWITCH_ZOOM).
POINT_POLY_SWITCH_ZOOM = 8
# Feature caps: about 1 MB of structuredContent each, which the host relays to the iframe over postMessage.
MAX_POLYGONS = 300
MAX_POINTS = 2000
VIEWPORT_PX = 800  # the map view's width, to turn a bounding box into a zoom
# Properties that only exist on drained lakes.
BREAK_FIELDS = (
    "date_break", "pre_break_median", "post_break_median", "water_change_ha", "water_change_perc",
    "date_break_year", "date_break_month",
)  # fmt: skip
PROPERTY_COLUMNS = (
    "id_geohash", "Area_start_ha", "Area_end_ha", "NetChange_ha", "NetChange_perc", "has_timeseries", *BREAK_FIELDS,
)  # fmt: skip


def zoom_for_bbox(bbox: BBox | None) -> int:
    """Web-mercator zoom at which ``bbox`` fills the map view (0 when there is no box: the whole layer)."""
    if bbox is None:
        return 0
    width = max(bbox[2] - bbox[0], 1e-6)
    return max(0, min(18, int(math.log2(360 * VIEWPORT_PX / 256 / width))))


@dataclass
class LakeResult:
    num_found: int  # lakes matching the filters
    drained: int  # of those, with a detected drainage break
    page: list[dict[str, Any]]  # attributes of the requested limit/offset page
    features: list[dict[str, Any]]  # GeoJSON features for the map, capped
    geometry: str  # "polygon" or "point"
    zoom: int


def _clean(props: dict[str, Any]) -> dict[str, Any]:
    if props.get("date_break_year") is None:
        for k in BREAK_FIELDS:
            props.pop(k, None)
    return props


def _rank(props: dict[str, Any]) -> tuple[int, bool, float, str]:
    """Drained lakes first by size of the water change, then stable lakes by area; missing values and ties last."""
    if props.get("date_break_year") is not None:
        change = props.get("water_change_ha")
        return 0, change is None, -abs(change or 0.0), props["id_geohash"]
    area = props.get("Area_start_ha")
    return 1, area is None, -(area or 0.0), props["id_geohash"]


class WaterData(ABC):
    """Lakes plus the per-lake time series."""

    def __init__(self, data_dir: str = "") -> None:
        self._dir = Path(data_dir).expanduser() if data_dir else None

    def _read(self, name: str) -> Any:
        base = self._dir if self._dir is not None else files("arcticdata_mcp") / "data" / "water"
        return json.loads((base / name).read_text(encoding="utf-8"))

    @cached_property
    def timeseries(self) -> dict[str, Any]:
        return self._read("timeseries.json")

    @abstractmethod
    def search(
        self,
        *,
        bbox: BBox | None,
        only_drained: bool,
        only_with_timeseries: bool,
        limit: int,
        offset: int,
        zoom: int | None = None,
    ) -> LakeResult: ...

    @abstractmethod
    def lake(self, lake_id: str) -> dict[str, Any] | None:
        """Properties of one lake."""


def _bounds(coords: Any) -> BBox:
    """(west, south, east, north) of a nested GeoJSON coordinate array."""
    xs: list[float] = []
    ys: list[float] = []

    def walk(c: Any) -> None:
        if isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        else:
            for part in c:
                walk(part)

    walk(coords)
    return min(xs), min(ys), max(xs), max(ys)


class JsonWaterData(WaterData):
    """lakes.geojson held in memory."""

    @cached_property
    def lakes(self) -> list[dict[str, Any]]:
        features = self._read("lakes.geojson")["features"]
        for f in features:
            f["bbox"] = _bounds(f["geometry"]["coordinates"])
        return sorted(features, key=lambda f: _rank(f["properties"]))

    def search(
        self,
        *,
        bbox: BBox | None,
        only_drained: bool,
        only_with_timeseries: bool,
        limit: int,
        offset: int,
        zoom: int | None = None,
    ) -> LakeResult:
        zoom = zoom_for_bbox(bbox) if zoom is None else zoom
        polygons = zoom >= POINT_POLY_SWITCH_ZOOM
        matches = []
        for f in self.lakes:  # already ranked
            props = f["properties"]
            if only_with_timeseries and not props["has_timeseries"]:
                continue
            if only_drained and props.get("date_break_year") is None:
                continue
            if bbox is not None:
                w, s, e, n = f["bbox"]
                if e < bbox[0] or w > bbox[2] or n < bbox[1] or s > bbox[3]:
                    continue
            matches.append(f)
        features = []
        for f in matches[: MAX_POLYGONS if polygons else MAX_POINTS]:
            if polygons:
                geometry = f["geometry"]
            else:
                w, s, e, n = f["bbox"]
                geometry = {"type": "Point", "coordinates": [(w + e) / 2, (s + n) / 2]}
            features.append({"type": "Feature", "properties": f["properties"], "geometry": geometry})
        return LakeResult(
            num_found=len(matches),
            drained=sum(1 for f in matches if f["properties"].get("date_break_year") is not None),
            page=[f["properties"] for f in matches[offset : offset + limit]],
            features=features,
            geometry="polygon" if polygons else "point",
            zoom=zoom,
        )

    def lake(self, lake_id: str) -> dict[str, Any] | None:
        return next((f["properties"] for f in self.lakes if f["properties"]["id_geohash"] == lake_id), None)


class ParquetWaterData(WaterData):
    """lakes.parquet queried with DuckDB (no spatial extension: the file carries bbox and centroid columns)."""

    @cached_property
    def _con(self) -> Any:
        import duckdb

        path = str((self._dir / "lakes.parquet").resolve()).replace("'", "''")
        con = duckdb.connect()
        con.execute(f"CREATE VIEW lakes AS SELECT * FROM read_parquet('{path}')")
        return con

    def _rows(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        cur = self._con.cursor()  # a cursor per call: the tools run in a thread pool
        cur.execute(sql, params)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]

    def search(
        self,
        *,
        bbox: BBox | None,
        only_drained: bool,
        only_with_timeseries: bool,
        limit: int,
        offset: int,
        zoom: int | None = None,
    ) -> LakeResult:
        zoom = zoom_for_bbox(bbox) if zoom is None else zoom
        polygons = zoom >= POINT_POLY_SWITCH_ZOOM
        clauses: list[str] = []
        params: list[Any] = []
        if bbox is not None:
            west, south, east, north = bbox
            clauses.append("east >= ? AND west <= ? AND north >= ? AND south <= ?")
            params += [west, east, south, north]
        if only_drained:
            clauses.append("date_break_year IS NOT NULL")
        if only_with_timeseries:
            clauses.append("has_timeseries")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = (
            "ORDER BY date_break_year IS NULL, "
            "CASE WHEN date_break_year IS NULL THEN Area_start_ha ELSE abs(water_change_ha) END DESC NULLS LAST, "
            "id_geohash"
        )
        cols = ", ".join(PROPERTY_COLUMNS)

        counts = self._rows(
            f"SELECT count(*) AS n, count(date_break_year) AS drained FROM lakes {where}", params
        )[0]
        page = self._rows(f"SELECT {cols} FROM lakes {where} {order} LIMIT ? OFFSET ?", [*params, limit, offset])
        geom = "geom" if polygons else "NULL AS geom"
        cap = MAX_POLYGONS if polygons else MAX_POINTS
        shown = self._rows(f"SELECT {cols}, lon, lat, {geom} FROM lakes {where} {order} LIMIT ?", [*params, cap])
        features = []
        for row in shown:
            lon, lat, geometry = row.pop("lon"), row.pop("lat"), row.pop("geom")
            features.append(
                {
                    "type": "Feature",
                    "properties": _clean(row),
                    "geometry": json.loads(geometry) if geometry else {"type": "Point", "coordinates": [lon, lat]},
                }
            )
        return LakeResult(
            num_found=counts["n"],
            drained=counts["drained"],
            page=[_clean(r) for r in page],
            features=features,
            geometry="polygon" if polygons else "point",
            zoom=zoom,
        )

    def lake(self, lake_id: str) -> dict[str, Any] | None:
        rows = self._rows(f"SELECT {', '.join(PROPERTY_COLUMNS)} FROM lakes WHERE id_geohash = ?", [lake_id])
        return _clean(rows[0]) if rows else None


def open_water_data(data_dir: str) -> WaterData:
    if data_dir and (Path(data_dir).expanduser() / "lakes.parquet").is_file():
        return ParquetWaterData(data_dir)
    return JsonWaterData(data_dir)
