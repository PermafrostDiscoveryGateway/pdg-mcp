"""MCP Apps UI resources (interactive views rendered by supporting clients)."""

from __future__ import annotations

from importlib.resources import files

from fastmcp import FastMCP
from fastmcp.apps import AppConfig, ResourceCSP

from arcticdata_mcp.tools.common import ToolContext

SEARCH_MAP_URI = "ui://arcticdata/search-map.html"

# Origins the sandboxed iframe may load scripts, styles and map tiles from.
SEARCH_MAP_CSP = ResourceCSP(resource_domains=["https://cdn.jsdelivr.net", "https://tile.openstreetmap.org"])


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    @mcp.resource(SEARCH_MAP_URI, name="search_map", app=AppConfig(csp=SEARCH_MAP_CSP))
    def search_map() -> str:
        """Leaflet map that draws the dataset footprints returned by search_by_bounding_box."""
        return (files("arcticdata_mcp") / "ui" / "search_map.html").read_text(encoding="utf-8")
