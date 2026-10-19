"""Water-timeseries MCP Apps proof of concept: lake polygons and Dynamic World water-area time series.

Serves a small pre-exported slice of the water-timeseries-v2 data (see scripts/export_water_fixtures.py).
Both tools render an interactive view in clients that support MCP Apps; others get the JSON result.
"""

from __future__ import annotations

import json
import math
from functools import cached_property
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.apps import AppConfig
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import TextContent, ToolAnnotations
from pydantic import Field

from arcticdata_mcp.tools.apps import LAKE_MAP_URI, LAKE_TIMESERIES_URI
from arcticdata_mcp.tools.common import ToolContext

# Local data only, so unlike the ADC tools these are not open-world.
LOCAL_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False, idempotent_hint=True)

OptLat = Annotated[float | None, Field(ge=-90, le=90, description="Latitude in decimal degrees (WGS84).")]
OptLon = Annotated[float | None, Field(ge=-180, le=180, description="Longitude in decimal degrees (WGS84).")]
LakeId = Annotated[
    str,
    Field(pattern=r"^[0-9a-z]{12}$", description="Lake id (12-character geohash, the id_geohash field), e.g. 'b7uefy0bvcrc'."),
]


def _bounds(coords: Any) -> tuple[float, float, float, float]:
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


class WaterData:
    """Lazily loaded lakes.geojson + timeseries.json."""

    def __init__(self, data_dir: str) -> None:
        self._dir = Path(data_dir).expanduser() if data_dir else None

    def _read(self, name: str) -> Any:
        if self._dir is not None:
            return json.loads((self._dir / name).read_text(encoding="utf-8"))
        return json.loads((files("arcticdata_mcp") / "data" / "water" / name).read_text(encoding="utf-8"))

    @cached_property
    def lakes(self) -> list[dict[str, Any]]:
        features = self._read("lakes.geojson")["features"]
        for f in features:
            f["bbox"] = list(_bounds(f["geometry"]["coordinates"]))
        return features

    @cached_property
    def timeseries(self) -> dict[str, Any]:
        return self._read("timeseries.json")

    def lake(self, lake_id: str) -> dict[str, Any] | None:
        return next((f for f in self.lakes if f["properties"]["id_geohash"] == lake_id), None)


def _summary(dates: list[str], water: list[float | None]) -> dict[str, Any]:
    obs = [(d, v) for d, v in zip(dates, water, strict=True) if v is not None and not math.isnan(v)]
    if not obs:
        return {"n_observations": 0}
    values = [v for _, v in obs]
    return {
        "n_observations": len(obs),
        "first": {"date": obs[0][0], "water_ha": obs[0][1]},
        "last": {"date": obs[-1][0], "water_ha": obs[-1][1]},
        "min": {"date": min(obs, key=lambda o: o[1])[0], "water_ha": min(values)},
        "max": {"date": max(obs, key=lambda o: o[1])[0], "water_ha": max(values)},
        "mean_water_ha": round(sum(values) / len(values), 3),
    }


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    data = WaterData(ctx.settings.water.data_dir)

    @mcp.tool(annotations=LOCAL_READ_ONLY, tags={"water", "spatial"}, app=AppConfig(resource_uri=LAKE_MAP_URI))
    def show_lakes(
        north: OptLat = None,
        south: OptLat = None,
        east: OptLon = None,
        west: OptLon = None,
        only_with_timeseries: Annotated[
            bool, Field(description="Only return lakes that have a water-area time series (get_lake_timeseries).")
        ] = False,
        limit: Annotated[int, Field(ge=1, le=500, description="Maximum number of lakes to return.")] = 200,
    ) -> ToolResult:
        """Show Arctic lake polygons from the water-timeseries dataset on an interactive map.

        Each lake has its area at the start and end of the record and the net change (ha and %). Omit the
        bounding box to get every lake. This proof of concept only holds a small sample of lakes on the
        Seward Peninsula, Alaska (about 66.3-66.8 N, 164.3-160.8 W); lakes with has_timeseries=true can be
        passed to get_lake_timeseries.
        """
        box = (west, south, east, north)
        if any(v is not None for v in box) and any(v is None for v in box):
            raise ToolError("Give all four of north, south, east and west, or none of them.")
        if north is not None and south is not None and south > north:
            raise ToolError("south must be <= north.")

        matches = []
        for f in data.lakes:
            if only_with_timeseries and not f["properties"]["has_timeseries"]:
                continue
            if north is not None:
                w, s, e, n = f["bbox"]
                if e < west or w > east or n < south or s > north:  # type: ignore[operator]
                    continue
            matches.append(f)

        shown = matches[:limit]
        lakes = [f["properties"] for f in shown]
        structured = {
            "numFound": len(matches),
            "returned": len(shown),
            "lakes": lakes,
            "geojson": {
                "type": "FeatureCollection",
                "features": [{k: f[k] for k in ("type", "properties", "geometry")} for f in shown],
            },
        }
        # The model gets the lake attributes; the polygons are only for the map view.
        text = json.dumps({"numFound": len(matches), "returned": len(shown), "lakes": lakes})
        return ToolResult(content=[TextContent(type="text", text=text)], structured_content=structured)

    @mcp.tool(
        annotations=LOCAL_READ_ONLY, tags={"water", "temporal"}, app=AppConfig(resource_uri=LAKE_TIMESERIES_URI)
    )
    def get_lake_timeseries(lake_id: LakeId) -> dict[str, Any]:
        """Monthly water area (ha) of one lake from Dynamic World, shown as an interactive chart.

        Covers June-September of each year. Also returns the areas of the other Dynamic World land-cover
        classes inside the lake polygon (bare, grass, snow_and_ice, ...), which help explain drops in water
        area. Use show_lakes(only_with_timeseries=true) to find lake ids that have data.
        """
        ts = data.timeseries
        series = ts["lakes"].get(lake_id)
        if series is None:
            available = ", ".join(sorted(ts["lakes"]))
            raise ToolError(f"No time series for lake {lake_id!r}. Lakes with data: {available}.")
        lake = data.lake(lake_id)
        return {
            "lake_id": lake_id,
            "source": ts["source"],
            "unit": ts["unit"],
            "lake": lake["properties"] if lake else None,
            "summary": _summary(ts["dates"], series["water"]),
            "dates": ts["dates"],
            "series": series,
        }
