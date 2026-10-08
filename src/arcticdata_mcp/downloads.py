"""Shared logic for delivering object bytes to the MCP client (inline, by URL, or saved on the server)."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp import types as mcp_types

from arcticdata_mcp.client import DataONEClient, as_list, quote_pid
from arcticdata_mcp.config import Settings

TEXTUAL_MIME = re.compile(r"^(text/|application/(json|xml|.*\+xml|x-yaml|yaml|csv|geo\+json|x-netcdf-cdl))")
HASHLIB_NAMES = {"MD5": "md5", "SHA-1": "sha1", "SHA-256": "sha256", "SHA-512": "sha512"}


async def object_info(client: DataONEClient, pid: str) -> dict[str, Any]:
    """Minimal facts about an object from its system metadata (GET /meta/{pid})."""
    sm = await client.get_xml(client.mn(f"meta/{quote_pid(pid)}"))
    checksum = sm.get("checksum") or {}
    return {
        "pid": sm.get("identifier", pid),
        "fileName": sm.get("fileName"),
        "formatId": sm.get("formatId"),
        "size": int(sm["size"]) if sm.get("size") else None,
        "checksum": checksum.get("#text") if isinstance(checksum, dict) else checksum,
        "checksumAlgorithm": checksum.get("@algorithm") if isinstance(checksum, dict) else None,
        "obsoletedBy": sm.get("obsoletedBy"),
        "archived": sm.get("archived") == "true",
        "publicRead": any(
            a.get("subject") == "public"
            for a in as_list((sm.get("accessPolicy") or {}).get("allow"))
            if isinstance(a, dict)
        ),
        "download_url": client.object_url(pid),
    }


def guess_mime(format_id: str | None, file_name: str | None) -> str:
    if format_id and "/" in format_id and not format_id.startswith("http"):
        return format_id
    if format_id and ("eml" in format_id or format_id.endswith("xml") or "iso" in format_id.lower()):
        return "application/xml"
    if file_name:
        guessed, _ = mimetypes.guess_type(file_name)
        if guessed:
            return guessed
    return "application/octet-stream"


def is_textual(mime: str) -> bool:
    return bool(TEXTUAL_MIME.match(mime))


def safe_filename(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "download"


def verify_checksum(data_hasher: Any, expected: str | None) -> bool | None:
    if expected is None or data_hasher is None:
        return None
    return data_hasher.hexdigest().lower() == expected.lower()


def inline_result(url: str, data: bytes, mime: str, info: dict[str, Any]) -> ToolResult:
    """Build a tool result that embeds the file bytes as an MCP embedded resource."""
    if is_textual(mime):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        resource: mcp_types.TextResourceContents | mcp_types.BlobResourceContents = (
            mcp_types.TextResourceContents(uri=url, mime_type=mime, text=text)
        )
    else:
        resource = mcp_types.BlobResourceContents(
            uri=url, mime_type=mime, blob=base64.b64encode(data).decode()
        )
    summary = (
        f"Downloaded '{info.get('fileName') or info.get('pid')}' ({len(data)} bytes, {mime}). "
        "File contents are attached as an embedded resource."
    )
    return ToolResult(
        content=[
            mcp_types.TextContent(type="text", text=summary),
            mcp_types.EmbeddedResource(type="resource", resource=resource),
        ],
        structured_content={**info, "delivery": "inline", "mime_type": mime, "bytes": len(data)},
    )


def url_result(info: dict[str, Any], settings: Settings, reason: str) -> dict[str, Any]:
    url = info["download_url"]
    name = safe_filename(info.get("fileName") or info.get("pid") or "download")
    return {
        **info,
        "delivery": "url",
        "reason": reason,
        "max_inline_bytes": settings.downloads.max_inline_bytes,
        "how_to_download": f"curl -L -o {name} '{url}'",
    }


async def fetch_bytes(client: DataONEClient, url: str, limit: int) -> bytes:
    """GET a URL, aborting if the body exceeds ``limit`` bytes."""
    chunks: list[bytes] = []
    total = 0
    async with client.stream(url) as resp:
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > limit:
                raise ToolError(f"Response exceeded inline limit of {limit} bytes; use the download URL instead: {url}")
            chunks.append(chunk)
    return b"".join(chunks)


async def save_to_server(
    client: DataONEClient,
    settings: Settings,
    url: str,
    file_name: str,
    checksum: str | None = None,
    algorithm: str | None = None,
) -> dict[str, Any]:
    """Stream a URL to the configured download directory on the MCP server host."""
    if not settings.downloads.allow_server_save:
        raise ToolError("Saving files on the server is disabled (downloads.allow_server_save = false).")
    out_dir = Path(settings.downloads.download_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = (out_dir / safe_filename(file_name)).resolve()
    if out_dir not in target.parents:
        raise ToolError("Refusing to write outside the download directory")
    hasher = hashlib.new(HASHLIB_NAMES[algorithm]) if algorithm in HASHLIB_NAMES else None
    total = 0
    tmp = target.with_suffix(target.suffix + ".part")
    async with client.stream(url) as resp:
        with tmp.open("wb") as fh:
            async for chunk in resp.aiter_bytes():
                fh.write(chunk)
                total += len(chunk)
                if hasher:
                    hasher.update(chunk)
    ok = verify_checksum(hasher, checksum)
    if ok is False:
        tmp.unlink(missing_ok=True)
        raise ToolError(f"Checksum mismatch for {url}; file discarded")
    tmp.replace(target)
    return {"delivery": "server_file", "path": str(target), "bytes": total, "checksum_verified": ok}
