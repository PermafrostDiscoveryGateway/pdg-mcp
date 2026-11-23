"""FastMCP application factory."""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastmcp import FastMCP

from arcticdata_mcp.activeusers import ActiveUsers
from arcticdata_mcp.auth import build_auth
from arcticdata_mcp.client import DataONEClient
from arcticdata_mcp.config import ObservabilitySettings, Settings
from arcticdata_mcp.metrics import Metrics
from arcticdata_mcp.observability import ToolCallLogMiddleware
from arcticdata_mcp.ratelimit import UserRateLimitMiddleware
from arcticdata_mcp.tools import apps, datasets, metadata_gaps, node, objects, portals, search, variables, water
from arcticdata_mcp.tools.common import ToolContext

log = logging.getLogger(__name__)

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


def build_active_users(obs: ObservabilitySettings, *, require_auth: bool) -> ActiveUsers | None:
    """The active-user store the settings ask for, or ``None`` when there is nothing to count.

    Without authentication every caller is anonymous, so there are no users to count. A database file is only
    used with a configured ``user_hash_key``: hashes from a random per-process key change on restart, so the same
    person would be counted again after every restart.
    """
    if not obs.active_users_enabled or not require_auth:
        return None
    path = obs.active_users_db
    if path and not obs.user_hash_key:
        log.warning(
            "observability.active_users_db is set but observability.user_hash_key is not: keeping active users in "
            "memory only, because hashes from a random key cannot be matched after a restart"
        )
        path = ""
    elif not path:
        log.warning("observability.active_users_db is not set: active-user counts restart at zero with the process")
    return ActiveUsers(path, obs.active_users_retention_days)


def create_server(
    settings: Settings,
    *,
    require_auth: bool = True,
    transport: httpx.AsyncBaseTransport | None = None,
    metrics: Metrics | None = None,
    active_users: ActiveUsers | None = None,
) -> FastMCP:
    """Build the MCP server. ``transport`` lets tests inject a mock HTTP transport.

    ``metrics`` is the collector set to update; the caller serves it (see ``start_metrics_server``).
    ``active_users`` overrides the store built from the settings (tests use it).
    """
    metrics = metrics or Metrics()
    client = DataONEClient(settings, transport=transport, metrics=metrics)
    if active_users is None:
        active_users = build_active_users(settings.observability, require_auth=require_auth)
    metrics.attach_active_users(active_users)

    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncIterator[None]:
        try:
            metrics.init_tools(tool.name for tool in await server.list_tools())
        except Exception:  # metrics are best-effort; never block startup
            log.warning('Could not register tool names for metrics; calls will be counted as "unknown"', exc_info=True)
        try:
            yield
        finally:
            await client.aclose()

    # One key for every hash in the logs (tool calls and auth rejections). Random per process if not configured.
    user_hash_key = settings.observability.user_hash_key or secrets.token_hex(32)
    auth = build_auth(settings.auth, user_hash_key) if require_auth else None
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
    mcp.add_middleware(ToolCallLogMiddleware(settings.observability, metrics, user_hash_key, active_users))
    mcp.add_middleware(UserRateLimitMiddleware(settings.ratelimit))
    ctx = ToolContext(settings=settings, client=client)
    for module in (search, variables, metadata_gaps, datasets, objects, node, portals, apps, water):
        module.register(mcp, ctx)
    if settings.tools.exclude_tags:
        mcp.disable(tags=set(settings.tools.exclude_tags), components={"tool"})
    return mcp
