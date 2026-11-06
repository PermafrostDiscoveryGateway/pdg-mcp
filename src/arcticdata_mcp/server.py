"""FastMCP application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastmcp import FastMCP

from arcticdata_mcp.auth import build_auth
from arcticdata_mcp.client import DataONEClient
from arcticdata_mcp.config import Settings
from arcticdata_mcp.metrics import Metrics
from arcticdata_mcp.observability import ToolCallLogMiddleware
from arcticdata_mcp.ratelimit import UserRateLimitMiddleware
from arcticdata_mcp.tools import apps, datasets, node, objects, portals, search, water
from arcticdata_mcp.tools.common import ToolContext

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Search and download public data from the NSF Arctic Data Center (arcticdata.io), a DataONE member node.

Typical workflow:
1. Discover datasets with search_datasets or a specific search_by_* tool (keyword, bounding box, temporal
   coverage, author, funding award, measured variable, taxon, ...). get_facet_counts reveals valid values.
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
    metrics: Metrics | None = None,
) -> FastMCP:
    """Build the MCP server. ``transport`` lets tests inject a mock HTTP transport.

    ``metrics`` is the collector set to update; the caller serves it (see ``start_metrics_server``).
    """
    metrics = metrics or Metrics()
    client = DataONEClient(settings, transport=transport, metrics=metrics)

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
    if auth is not None and settings.observability.log_tool_calls and not settings.observability.user_hash_key:
        log.warning(
            "observability.user_hash_key is not set: using a random key for this process, so user_hash "
            "values in logs change on restart and differ between pods"
        )
    # Logging goes first (outermost) so it also records calls the rate limiter rejects.
    mcp.add_middleware(ToolCallLogMiddleware(settings.observability, metrics))
    mcp.add_middleware(UserRateLimitMiddleware(settings.ratelimit))
    ctx = ToolContext(settings=settings, client=client)
    for module in (search, datasets, objects, node, portals, apps, water):
        module.register(mcp, ctx)
    return mcp
