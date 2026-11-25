"""FastMCP application factory."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastmcp import FastMCP

from arcticdata_mcp import solr
from arcticdata_mcp.auth import build_auth
from arcticdata_mcp.client import DataONEClient
from arcticdata_mcp.config import Settings
from arcticdata_mcp.ratelimit import UserRateLimitMiddleware
from arcticdata_mcp.tools import (
    apps,
    datasets,
    fields_in_use,
    metadata_gaps,
    node,
    objects,
    portals,
    search,
    variables,
    water,
)
from arcticdata_mcp.tools.common import ToolContext

INSTRUCTIONS = """\
Search and download public data from the NSF Arctic Data Center (arcticdata.io), a DataONE member node.

Typical workflow:
1. Discover datasets with search_datasets, combining any of its filters (text, keyword, author, bounding box,
   collection period, funding award, measured variable, taxon, ...). get_facet_counts reveals valid values;
   search_data_files finds individual files; find_dataset_by_identifier resolves a DOI or file PID.
   For "datasets that measure X" use search_by_variable: it maps X to ECSO ontology concepts and searches
   attribute annotations, which finds far more than free text. find_variable_concepts shows the candidates.
2. Inspect a dataset with get_dataset_details, get_metadata_document (EML) or get_dataset_citation.
3. List its files with list_dataset_files, peek with preview_file, then fetch with download_file.
   Large files return a direct URL instead of inline bytes (see get_server_config).
Counts cover the latest version of each dataset unless latest_only=false (older versions are excluded).
Project portals (curated collections such as PerCS-Net or the Permafrost Discovery Gateway) add a project
layer: search_portals finds them, get_portal describes one, search_portal_datasets searches exactly the
datasets it collects, and find_portals_for_dataset lists the portals that include a dataset.
To find datasets with gaps in their metadata (no attributes, keywords, coverage, funding, ...), use
search_missing_metadata.
Identifiers (PIDs) look like 'doi:10.18739/A2...' for datasets and 'urn:uuid:...' for files.

Separately, show_lakes and get_lake_timeseries serve a small sample of Arctic lake water-area data
(water-timeseries-v2, Dynamic World) with interactive map and chart views.
"""
log = logging.getLogger(__name__)


async def warn_on_field_drift(client: DataONEClient) -> list[str]:
    """Log a warning for each index field name we use that the index no longer offers. Never raises."""
    try:
        problems = solr.check_fields(await solr.fetch_index_fields(client), fields_in_use())
    except Exception as exc:  # noqa: BLE001 - a failed check must not affect the server
        log.warning("Could not check Solr field names against the index: %s", exc)
        return []
    for problem in problems:
        log.warning("Solr field name out of sync with the index: %s", problem)
    return problems


def create_server(
    settings: Settings,
    *,
    require_auth: bool = True,
    transport: httpx.AsyncBaseTransport | None = None,
    check_index_fields: bool = False,
) -> FastMCP:
    """Build the MCP server. ``transport`` lets tests inject a mock HTTP transport.

    ``check_index_fields`` compares the Solr field names we use with the index once at startup, in the
    background, and logs a warning for each mismatch.
    """
    client = DataONEClient(settings, transport=transport)

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[None]:
        check = asyncio.create_task(warn_on_field_drift(client)) if check_index_fields else None
        try:
            yield
        finally:
            try:
                if check:
                    check.cancel()
                    await asyncio.wait([check])
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
    for module in (search, variables, metadata_gaps, datasets, objects, node, portals, apps, water):
        module.register(mcp, ctx)
    if settings.tools.exclude_tags:
        mcp.disable(tags=set(settings.tools.exclude_tags), components={"tool"})
    return mcp
