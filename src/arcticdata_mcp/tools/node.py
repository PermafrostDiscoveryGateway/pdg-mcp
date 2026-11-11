"""Node-level and reference tools: capabilities, health, query engines, views, object formats, server config."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from arcticdata_mcp.client import as_list, quote_pid
from arcticdata_mcp.tools.common import READ_ONLY, ToolContext


def _format(f: dict[str, Any]) -> dict[str, Any]:
    media = f.get("mediaType")
    return {
        "formatId": f.get("formatId"),
        "formatName": f.get("formatName"),
        "formatType": f.get("formatType"),
        "mediaType": media.get("@name") if isinstance(media, dict) else media,
        "extension": f.get("extension"),
    }


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    client = ctx.client
    settings = ctx.settings

    @mcp.tool(annotations=READ_ONLY, tags={"node", "low_level"})
    async def get_node_info() -> dict[str, Any]:
        """Describe the Arctic Data Center member node (GET /node): identifier, description, supported
        DataONE services/versions, sync schedule, and software version.
        """
        n = await client.get_xml(client.mn("node"))
        return {
            "identifier": n.get("identifier"),
            "name": n.get("name"),
            "description": n.get("description"),
            "baseURL": n.get("baseURL"),
            "state": n.get("@state"),
            "type": n.get("@type"),
            "services": [
                {"name": s.get("@name"), "version": s.get("@version"), "available": s.get("@available") == "true"}
                for s in as_list((n.get("services") or {}).get("service"))
            ],
            "properties": {p.get("@key"): p.get("#text") for p in as_list(n.get("property")) if isinstance(p, dict)},
            "synchronization": n.get("synchronization"),
            "contactSubject": n.get("contactSubject"),
        }

    @mcp.tool(annotations=READ_ONLY, tags={"node", "low_level"})
    async def ping() -> dict[str, Any]:
        """Check that the Arctic Data Center API is up (GET /monitor/ping)."""
        resp = await client.request("GET", client.mn("monitor/ping"), raise_errors=False)
        return {"up": resp.status_code == 200, "status_code": resp.status_code, "date": resp.headers.get("date")}

    @mcp.tool(annotations=READ_ONLY, tags={"node", "low_level"})
    async def list_query_engines() -> dict[str, Any]:
        """List the query engines the node supports (GET /query). The Arctic Data Center uses 'solr'."""
        q = await client.get_xml(client.mn("query"))
        return {"query_engines": as_list(q.get("queryEngine"))}

    @mcp.tool(annotations=READ_ONLY, tags={"node", "low_level"})
    async def list_views() -> dict[str, Any]:
        """List metadata rendering views the node supports (GET /views), usable with get_metadata_html."""
        v = await client.get_xml(client.mn("views"))
        return {"views": as_list(v.get("option")), "description": v.get("@description")}

    @mcp.tool(annotations=READ_ONLY, tags={"reference", "low_level"})
    async def list_object_formats(
        format_type: Annotated[
            Literal["any", "DATA", "METADATA", "RESOURCE"],
            Field(description="Restrict to data, metadata or resource formats."),
        ] = "any",
        contains: Annotated[str | None, Field(description="Case-insensitive filter on format ID, name, media type or extension.")] = None,
    ) -> dict[str, Any]:
        """List DataONE object format IDs (from the DataONE Coordinating Node GET /formats), e.g. to pick
        a formatId for search_data_files.
        """
        fl = await client.get_xml(client.cn("formats"))
        formats = [_format(f) for f in as_list(fl.get("objectFormat"))]
        if format_type != "any":
            formats = [f for f in formats if f["formatType"] == format_type]
        if contains:
            c = contains.lower()
            formats = [f for f in formats if any(c in str(v).lower() for v in f.values() if v)]
        return {"count": len(formats), "formats": formats}

    @mcp.tool(annotations=READ_ONLY, tags={"reference", "low_level"})
    async def get_object_format(
        format_id: Annotated[str, Field(description="DataONE format ID, e.g. 'text/csv' or 'https://eml.ecoinformatics.org/eml-2.2.0'.")],
    ) -> dict[str, Any]:
        """Describe one DataONE object format (CN GET /formats/{formatId}): name, type, media type, extension."""
        return _format(await client.get_xml(client.cn(f"formats/{quote_pid(format_id)}")))

    @mcp.tool(annotations=READ_ONLY, tags={"reference"})
    async def get_server_config() -> dict[str, Any]:
        """Describe this MCP server's behavior: API endpoints used, download limits (max inline size,
        whether files can be saved on the server) and rate limits.
        """
        return {
            "member_node": settings.arcticdata.base_url,
            "coordinating_node": settings.arcticdata.cn_url,
            "catalog": settings.arcticdata.catalog_url,
            "max_inline_bytes": settings.downloads.max_inline_bytes,
            "allow_server_save": settings.downloads.allow_server_save,
            "download_dir": settings.downloads.download_dir if settings.downloads.allow_server_save else None,
            "access": "public, read-only content only",
            "rate_limits": {
                "per_user_calls_per_second": settings.ratelimit.per_user_rate or None,
                "per_user_burst": settings.ratelimit.per_user_burst if settings.ratelimit.per_user_rate else None,
                "max_concurrent_upstream_requests": settings.ratelimit.max_concurrent or None,
            },
        }
