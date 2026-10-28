"""Water-timeseries MCP Apps proof of concept: the "Lost Lakes" drainage map and lake time series.

Serves water-timeseries-v2 lakes from a DuckDB-queried GeoParquet store (scripts/build_water_store.py) or the
small bundled fixture, plus per-lake time series that scripts/export_water_fixtures.py precomputes with the
water_timeseries library (masking, breaks and the dashboard's Plotly figures). Both tools render an
interactive view in clients that support MCP Apps; others get the JSON result.
"""

from __future__ import annotations

import json
import math
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.apps import AppConfig
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import TextContent, ToolAnnotations
from pydantic import Field

from arcticdata_mcp.tools.apps import LAKE_MAP_URI, LAKE_TIMESERIES_URI
from arcticdata_mcp.tools.common import ToolContext
from arcticdata_mcp.waterdata import MAX_POINTS, MAX_POLYGONS, LakeResult, open_water_data

# Local data only, so unlike the ADC tools these are not open-world.
LOCAL_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False, idempotent_hint=True)

OptLat = Annotated[float | None, Field(ge=-90, le=90, description="Latitude in decimal degrees (WGS84).")]
OptLon = Annotated[float | None, Field(ge=-180, le=180, description="Longitude in decimal degrees (WGS84).")]
LakeId = Annotated[
    str,
    Field(pattern=r"^[0-9a-z]{12}$", description="Lake id (12-character geohash, the id_geohash field), e.g. 'b7uefy0bvcrc'."),
]


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


def _map_view(r: LakeResult) -> dict[str, Any]:
    """What the map view draws: capped GeoJSON plus the counts and a note when lakes were left out."""
    note = None
    if len(r.features) < r.num_found:
        cap = MAX_POLYGONS if r.geometry == "polygon" else MAX_POINTS
        note = f"Map shows the top {cap} of {r.num_found} lakes"
        note += "; zoom in to see more." if r.geometry == "point" else "; zoom in or filter to see others."
    return {
        "numFound": r.num_found,
        "drained": r.drained,
        "shown": len(r.features),
        "geometry": r.geometry,
        "map_note": note,
        "geojson": {"type": "FeatureCollection", "features": r.features},
    }


def _text(payload: dict[str, Any]) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload))]


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    data = open_water_data(ctx.settings.water.data_dir)

    @mcp.tool(annotations=LOCAL_READ_ONLY, tags={"water", "spatial"}, app=AppConfig(resource_uri=LAKE_MAP_URI))
    def show_lakes(
        north: OptLat = None,
        south: OptLat = None,
        east: OptLon = None,
        west: OptLon = None,
        only_drained: Annotated[
            bool, Field(description="Only return lakes with a detected drainage break (hide stable lakes).")
        ] = False,
        only_with_timeseries: Annotated[
            bool, Field(description="Only return lakes that have a water-area time series (get_lake_timeseries).")
        ] = False,
        limit: Annotated[int, Field(ge=1, le=500, description="Lakes listed in the result (page size).")] = 50,
        offset: Annotated[int, Field(ge=0, description="Lakes to skip, for paging through the list.")] = 0,
    ) -> ToolResult:
        """Show Arctic lakes on the "Lost Lakes" drainage map (the water-timeseries dashboard's Historical view).

        Drained lakes are colored by the year of their drainage break and carry the break date, the median
        water area before and after the break and the change of water area. Every lake also has its area in
        2000 and 2020 and the net change (Area_start_ha, Area_end_ha, NetChange_ha, NetChange_perc). Omit the
        bounding box to get every lake (west > east gives a box across the 180th meridian). Lakes are ranked drained-first (largest water change first), then
        stable lakes by size. The result lists `limit` lakes from `offset` and the total counts; the map shows
        the top lakes of the same ranking, as dots when the box is wide and as polygons when it is zoomed in,
        so give a bounding box to see more of a dense area. Lakes with has_timeseries=true can be passed to
        get_lake_timeseries.
        """
        box = (west, south, east, north)
        if any(v is not None for v in box) and any(v is None for v in box):
            raise ToolError("Give all four of north, south, east and west, or none of them.")
        if north is not None and south is not None and south > north:
            raise ToolError("south must be <= north.")

        r = data.search(
            bbox=None if west is None else box,  # type: ignore[arg-type]
            only_drained=only_drained,
            only_with_timeseries=only_with_timeseries,
            limit=limit,
            offset=offset,
        )
        summary: dict[str, Any] = {
            "numFound": r.num_found,
            "drained": r.drained,
            "returned": len(r.page),
            "offset": offset,
            "lakes": r.page,
        }
        if offset + len(r.page) < r.num_found:
            summary["next_offset"] = offset + len(r.page)
        structured = {**summary, **_map_view(r)}
        # The model gets the lake attributes; the geometry is only for the map view.
        return ToolResult(content=_text(summary), structured_content=structured)

    @mcp.tool(
        annotations=LOCAL_READ_ONLY, tags={"water", "spatial"}, app=AppConfig(visibility=["app"])
    )
    def lakes_in_view(
        north: Annotated[float, Field(ge=-90, le=90, description="Top of the map view (WGS84).")],
        south: Annotated[float, Field(ge=-90, le=90, description="Bottom of the map view (WGS84).")],
        east: Annotated[float, Field(ge=-180, le=180, description="Right edge of the map view (WGS84).")],
        west: Annotated[
            float,
            Field(ge=-180, le=180, description="Left edge of the map view (WGS84); west > east crosses 180 degrees."),
        ],
        zoom: Annotated[int, Field(ge=0, le=22, description="The map's zoom level; picks dots or polygons.")],
        only_drained: bool = False,
        only_with_timeseries: bool = False,
    ) -> ToolResult:
        """The lake map's pan/zoom reload: lakes in the current view, for the map only (hidden from the model)."""
        if south > north:
            raise ToolError("south must be <= north.")
        r = data.search(
            bbox=(west, south, east, north),
            only_drained=only_drained,
            only_with_timeseries=only_with_timeseries,
            limit=0,
            offset=0,
            zoom=zoom,
        )
        view = _map_view(r)
        return ToolResult(content=_text({k: view[k] for k in ("numFound", "drained", "shown")}), structured_content=view)

    @mcp.tool(
        annotations=LOCAL_READ_ONLY, tags={"water", "temporal"}, app=AppConfig(resource_uri=LAKE_TIMESERIES_URI)
    )
    def get_lake_timeseries(lake_id: LakeId) -> ToolResult:
        """Water-area time series of one lake, shown as the water-timeseries dashboard's interactive charts.

        Dynamic World (monthly, June-September): water, vegetation (flooded vegetation + grass + shrub/scrub
        + trees), bare and snow/ice area in ha, with invalid months (no-data or snow) already masked out.
        JRC Global Surface Water (annual): permanent and seasonal water and land. If a drainage break was
        detected, the break date and the median water area before/after it are included. Use
        show_lakes(only_with_timeseries=true) to find lake ids that have data.
        """
        entry = data.timeseries["lakes"].get(lake_id)
        if entry is None:
            available = ", ".join(sorted(data.timeseries["lakes"]))
            raise ToolError(f"No time series for lake {lake_id!r}. Lakes with data: {available}.")
        lake = data.lake(lake_id)
        dw = entry["dw"]
        summary: dict[str, Any] = {
            "lake_id": lake_id,
            "name": entry["name"],
            "lake": lake,
            "break": entry["break"],
            "summary": _summary(dw["data"]["dates"], dw["data"]["water"]),
            "dw": {k: v for k, v in dw.items() if k != "figure"},
        }
        if "jrc" in entry:
            summary["jrc"] = {k: v for k, v in entry["jrc"].items() if k != "figure"}
        structured = {
            **summary,
            "dw": dw,
            **({"jrc": entry["jrc"]} if "jrc" in entry else {}),
        }
        # The model gets the data; the Plotly figures are only for the chart view.
        return ToolResult(content=_text(summary), structured_content=structured)
