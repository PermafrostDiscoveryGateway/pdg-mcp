"""Object-level tools (MNRead / MNAuthorization): download, preview, describe, system metadata, checksums, listing."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from pydantic import Field

from arcticdata_mcp import downloads
from arcticdata_mcp.client import as_list, quote_pid
from arcticdata_mcp.solr import solr_date
from arcticdata_mcp.tools.common import READ_ONLY, DateStr, Pid, ToolContext


def _simplify_sysmeta(sm: dict[str, Any]) -> dict[str, Any]:
    access = [
        {"subject": a.get("subject"), "permissions": as_list(a.get("permission"))}
        for a in as_list((sm.get("accessPolicy") or {}).get("allow"))
        if isinstance(a, dict)
    ]
    rp = sm.get("replicationPolicy") or {}
    checksum = sm.get("checksum") or {}
    return {
        "identifier": sm.get("identifier"),
        "seriesId": sm.get("seriesId"),
        "fileName": sm.get("fileName"),
        "formatId": sm.get("formatId"),
        "mediaType": (sm.get("mediaType") or {}).get("@name") if isinstance(sm.get("mediaType"), dict) else None,
        "size": int(sm["size"]) if sm.get("size") else None,
        "checksum": {"algorithm": checksum.get("@algorithm"), "value": checksum.get("#text")} if isinstance(checksum, dict) else checksum,
        "submitter": sm.get("submitter"),
        "rightsHolder": sm.get("rightsHolder"),
        "accessPolicy": access,
        "replicationPolicy": {
            "replicationAllowed": rp.get("@replicationAllowed"),
            "numberReplicas": rp.get("@numberReplicas"),
            "preferred": as_list(rp.get("preferredMemberNode")),
            "blocked": as_list(rp.get("blockedMemberNode")),
        } if rp else None,
        "replicas": [
            {"node": r.get("replicaMemberNode"), "status": r.get("replicationStatus"), "verified": r.get("replicaVerified")}
            for r in as_list(sm.get("replica")) if isinstance(r, dict)
        ],
        "obsoletes": sm.get("obsoletes"),
        "obsoletedBy": sm.get("obsoletedBy"),
        "archived": sm.get("archived") == "true",
        "dateUploaded": sm.get("dateUploaded"),
        "dateSysMetadataModified": sm.get("dateSysMetadataModified"),
        "originMemberNode": sm.get("originMemberNode"),
        "authoritativeMemberNode": sm.get("authoritativeMemberNode"),
    }


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    client = ctx.client
    settings = ctx.settings

    @mcp.tool(annotations=READ_ONLY, tags={"files", "download"})
    async def download_file(
        pid: Annotated[str, Field(description="PID of the data file (from list_dataset_files or search_data_files).")],
        delivery: Annotated[
            Literal["auto", "inline", "url", "server"],
            Field(description=(
                "'auto' = return contents inline if within the size limit, else the download URL; "
                "'inline' = require inline contents (errors if too large); 'url' = only the URL; "
                "'server' = save to the MCP server's download directory (if enabled)."
            )),
        ] = "auto",
    ) -> ToolResult:
        """Download a raw file (MNRead GET /object/{pid}) and deliver its bytes to the agent.

        Small files come back inline as an embedded resource (text for CSV/XML/JSON etc., base64 blob otherwise).
        Files larger than the server's max_inline_bytes come back as a direct URL plus checksum so the
        agent can fetch them itself (e.g. with curl). See get_server_config for the current limit.
        """
        info = await downloads.object_info(client, pid)
        url = info["download_url"]
        limit = settings.downloads.max_inline_bytes
        size = info.get("size")
        if delivery == "url":
            return ToolResult(structured_content=downloads.url_result(info, settings, "URL requested"))
        if delivery == "server":
            saved = await downloads.save_to_server(
                client, settings, url, info.get("fileName") or pid, info.get("checksum"), info.get("checksumAlgorithm")
            )
            return ToolResult(structured_content={**info, **saved})
        if size is not None and size > limit:
            if delivery == "inline":
                raise ToolError(f"File is {size} bytes, above the inline limit of {limit} bytes. Use delivery='url'.")
            return ToolResult(structured_content=downloads.url_result(info, settings, f"File size {size} bytes exceeds inline limit"))
        data = await downloads.fetch_bytes(client, url, limit)
        mime = downloads.guess_mime(info.get("formatId"), info.get("fileName"))
        return downloads.inline_result(url, data, mime, info)

    @mcp.tool(annotations=READ_ONLY, tags={"files"})
    async def preview_file(
        pid: Annotated[str, Field(description="PID of the data file.")],
        max_bytes: Annotated[int, Field(ge=256, le=1_048_576, description="Read at most this many bytes from the start of the file.")] = 16_384,
        max_lines: Annotated[int | None, Field(ge=1, le=10_000, description="Optionally stop after this many lines.")] = 50,
    ) -> dict[str, Any]:
        """Peek at the first bytes/lines of a file without downloading all of it — ideal for checking CSV headers,
        text formats, or file signatures of large files.
        """
        url = client.object_url(pid)
        head_resp = await client.head(url)
        total_size = int(head_resp.headers["content-length"]) if head_resp.headers.get("content-length") else None
        fmt = head_resp.headers.get("dataone-objectformat")
        buf = bytearray()
        async with client.stream(url) as resp:
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if len(buf) >= max_bytes:
                    break
        head = bytes(buf[:max_bytes])
        binary = b"\x00" in head[:4096]
        out: dict[str, Any] = {"pid": pid, "formatId": fmt, "total_size": total_size, "bytes_read": len(head)}
        if binary:
            out["binary"] = True
            out["hex_head"] = head[:256].hex(" ")
            out["note"] = "Binary content; use download_file to retrieve it."
            return out
        text = head.decode("utf-8", errors="replace")
        lines = text.splitlines()
        truncated_lines = max_lines is not None and len(lines) > max_lines
        if truncated_lines:
            lines = lines[:max_lines]
        out.update({
            "binary": False,
            "lines_returned": len(lines),
            "truncated": truncated_lines or (total_size is not None and len(head) < total_size),
            "preview": "\n".join(lines),
        })
        return out

    @mcp.tool(annotations=READ_ONLY, tags={"files", "download"})
    async def get_download_url(pid: Pid) -> dict[str, Any]:
        """Return the direct download URL for an object along with its file name, size, format and checksum,
        so the agent can fetch it with its own HTTP client.
        """
        return downloads.url_result(await downloads.object_info(client, pid), settings, "URL requested")

    @mcp.tool(annotations=READ_ONLY, tags={"files"})
    async def describe_object(pid: Pid) -> dict[str, Any]:
        """Lightweight existence/size/format/checksum check via MNRead describe (HEAD /object/{pid})."""
        resp = await client.head(client.object_url(pid))
        h = resp.headers
        checksum = h.get("dataone-checksum")
        algo, _, value = (checksum or "").partition(",")
        return {
            "pid": pid,
            "exists": True,
            "formatId": h.get("dataone-objectformat"),
            "size": int(h["content-length"]) if h.get("content-length") else None,
            "checksum": {"algorithm": algo, "value": value} if checksum else None,
            "last_modified": h.get("last-modified"),
            "serial_version": h.get("dataone-serialversion"),
        }

    @mcp.tool(annotations=READ_ONLY, tags={"files", "metadata"})
    async def get_system_metadata(
        pid: Pid,
        raw: Annotated[bool, Field(description="Return the full parsed system metadata instead of the simplified view.")] = False,
    ) -> dict[str, Any]:
        """Get DataONE system metadata (GET /meta/{pid}): owner, submitter, access policy, replicas,
        checksum, size, format, file name, and version links (obsoletes/obsoletedBy).
        """
        sm = await client.get_xml(client.mn(f"meta/{quote_pid(pid)}"))
        return sm if raw else _simplify_sysmeta(sm)

    @mcp.tool(annotations=READ_ONLY, tags={"files"})
    async def get_checksum(
        pid: Pid,
        algorithm: Annotated[Literal["MD5", "SHA-1", "SHA-256"], Field(description="Checksum algorithm.")] = "SHA-256",
    ) -> dict[str, Any]:
        """Compute/return an object's checksum with the requested algorithm (GET /checksum/{pid}).
        Use it to verify files downloaded outside the MCP server.
        """
        cs = await client.get_xml(client.mn(f"checksum/{quote_pid(pid)}"), params={"checksumAlgorithm": algorithm})
        return {"pid": pid, "algorithm": cs.get("@algorithm", algorithm), "checksum": cs.get("#text")}

    @mcp.tool(annotations=READ_ONLY, tags={"files", "listing"})
    async def list_objects(
        from_date: DateStr = None,
        to_date: DateStr = None,
        format_id: Annotated[str | None, Field(description="Only objects with this DataONE format ID, e.g. 'text/csv'.")] = None,
        identifier: Annotated[str | None, Field(description="Only objects with this identifier (or series ID).")] = None,
        start: Annotated[int, Field(ge=0)] = 0,
        count: Annotated[int, Field(ge=0, le=1000)] = 50,
    ) -> dict[str, Any]:
        """List objects on the node by system-metadata modification date and/or format (MNRead GET /object).

        Useful for harvesting or "what changed since X". Returns identifier, format, size, checksum and modified date.
        """
        if any(d and d.strip().upper().startswith("NOW") for d in (from_date, to_date)):
            raise ToolError("list_objects needs absolute dates (e.g. YYYY-MM-DD), not NOW-math")
        params: dict[str, Any] = {"start": start, "count": count}
        if from_date:
            params["fromDate"] = solr_date(from_date).replace("Z", "+00:00")
        if to_date:
            params["toDate"] = solr_date(to_date, end_of_period=True).replace("Z", "+00:00")
        if format_id:
            params["formatId"] = format_id
        if identifier:
            params["identifier"] = identifier
        ol = await client.get_xml(client.mn("object"), params=params)
        items = []
        for o in as_list(ol.get("objectInfo")):
            cs = o.get("checksum") or {}
            items.append({
                "identifier": o.get("identifier"),
                "formatId": o.get("formatId"),
                "size": int(o["size"]) if o.get("size") else None,
                "checksum": {"algorithm": cs.get("@algorithm"), "value": cs.get("#text")} if isinstance(cs, dict) else cs,
                "dateSysMetadataModified": o.get("dateSysMetadataModified"),
            })
        return {"total": int(ol.get("@total", 0)), "start": int(ol.get("@start", 0)), "count": len(items), "objects": items}

    @mcp.tool(annotations=READ_ONLY, tags={"files", "auth"})
    async def is_authorized(
        pid: Pid,
        action: Annotated[Literal["read", "write", "changePermission"], Field(description="Permission to check.")] = "read",
    ) -> dict[str, Any]:
        """Check whether an anonymous (public) user may perform an action on an object (GET /isAuthorized/{pid}).
        Mostly useful to confirm a file is publicly downloadable.
        """
        resp = await client.request(
            "GET", client.mn(f"isAuthorized/{quote_pid(pid)}"), params={"action": action}, raise_errors=False
        )
        if resp.status_code == 200:
            return {"pid": pid, "action": action, "authorized": True}
        if resp.status_code in (401, 403):
            return {"pid": pid, "action": action, "authorized": False}
        if resp.status_code == 404:
            raise ToolError(f"Object '{pid}' not found")
        raise ToolError(f"isAuthorized returned HTTP {resp.status_code}: {resp.text[:300]}")
