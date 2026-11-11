"""FastMCP application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastmcp import FastMCP

from arcticdata_mcp.auth import build_auth
from arcticdata_mcp.client import DataONEClient
from arcticdata_mcp.config import Settings
from arcticdata_mcp.ratelimit import UserRateLimitMiddleware
from arcticdata_mcp.tools import apps, datasets, node, objects, portals, search, water
from arcticdata_mcp.tools.common import ToolContext

INSTRUCTIONS = """\
Search and download public data from the NSF Arctic Data Center (arcticdata.io), a DataONE member node.

Typical workflow:
1. Discover datasets with search_datasets, combining any of its filters (text, keyword, author, bounding box,
   collection period, funding award, measured variable, taxon, ...). get_facet_counts reveals valid values;
   search_data_files finds individual files; find_dataset_by_identifier resolves a DOI or file PID.
2. Inspect a dataset with get_dataset_details, get_metadata_document (EML) or get_dataset_citation.
3. List its files with list_dataset_files, peek with preview_file, then fetch with download_file.
   Large files return a direct URL instead of inline bytes (see get_server_config).
Project portals (curated collections such as PerCS-Net or the Permafrost Discovery Gateway) add a project
layer: search_portals finds them, get_portal describes one, search_portal_datasets searches exactly the
datasets it collects, and find_portals_for_dataset lists the portals that include a dataset.
Identifiers (PIDs) look like 'doi:10.18739/A2...' for datasets and 'urn:uuid:...' for files.

Separately, show_lakes and get_lake_timeseries serve a small sample of Arctic lake water-area data
(water-timeseries-v2, Dynamic World) with interactive map and chart views.
"""


def create_server(
    settings: Settings,
    *,
    require_auth: bool = True,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastMCP:
    """Build the MCP server. ``transport`` lets tests inject a mock HTTP transport."""
    client = DataONEClient(settings, transport=transport)

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await client.aclose()

    auth = build_auth(settings.auth) if require_auth else None
    mcp = FastMCP(
        name="Arctic Data Center",
        instructions=INSTRUCTIONS,
        auth=auth,
        lifespan=lifespan,
    )
    mcp.add_middleware(UserRateLimitMiddleware(settings.ratelimit))
    ctx = ToolContext(settings=settings, client=client)
    for module in (search, datasets, objects, node, portals, apps, water):
        module.register(mcp, ctx)
    if settings.tools.exclude_tags:
        mcp.disable(tags=set(settings.tools.exclude_tags), components={"tool"})
    return mcp
