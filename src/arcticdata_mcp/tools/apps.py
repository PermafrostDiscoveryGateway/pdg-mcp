"""MCP Apps UI resources (interactive views rendered by supporting clients)."""

from __future__ import annotations

from importlib.resources import files

from fastmcp import FastMCP
from fastmcp.apps import AppConfig, ResourceCSP

from arcticdata_mcp.tools.common import ToolContext

SEARCH_MAP_URI = "ui://arcticdata/search-map.html"
LAKE_MAP_URI = "ui://water/lake-map.html"
LAKE_TIMESERIES_URI = "ui://water/lake-timeseries.html"

# Origins the sandboxed iframes may load scripts, styles and map tiles from.
SEARCH_MAP_CSP = ResourceCSP(resource_domains=["https://cdn.jsdelivr.net", "https://tile.openstreetmap.org"])
LAKE_MAP_CSP = ResourceCSP(
    resource_domains=[
        "https://cdn.jsdelivr.net",
        "https://server.arcgisonline.com",  # ESRI World Imagery
        "https://*.basemaps.cartocdn.com",  # Dark Matter
        "https://maps.awi.de",  # TCVIS WMS
    ]
)
LAKE_TIMESERIES_CSP = ResourceCSP(resource_domains=["https://cdn.jsdelivr.net"])


def _page(name: str) -> str:
    return (files("arcticdata_mcp") / "ui" / name).read_text(encoding="utf-8")


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    @mcp.resource(SEARCH_MAP_URI, name="search_map", app=AppConfig(csp=SEARCH_MAP_CSP))
    def search_map() -> str:
        """Leaflet map that draws the dataset footprints returned by search_by_bounding_box."""
        return _page("search_map.html")

    @mcp.resource(LAKE_MAP_URI, name="lake_map", app=AppConfig(csp=LAKE_MAP_CSP))
    def lake_map() -> str:
        """Leaflet map of the lake polygons returned by show_lakes, colored by net area change."""
        return _page("lake_map.html")

    @mcp.resource(LAKE_TIMESERIES_URI, name="lake_timeseries", app=AppConfig(csp=LAKE_TIMESERIES_CSP))
    def lake_timeseries() -> str:
        """Plotly chart of the water-area time series returned by get_lake_timeseries."""
        return _page("lake_timeseries.html")
