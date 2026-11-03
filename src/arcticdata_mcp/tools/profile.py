"""Data-file profiling for metadata enrichment: summarize what a file contains so it can be compared with its EML."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from fastmcp.exceptions import ToolError

from arcticdata_mcp import downloads, profiling, profiling_netcdf, profiling_raster
from arcticdata_mcp.client import DataONEClient
from arcticdata_mcp.config import ProfileSettings
from arcticdata_mcp.tools.common import READ_ONLY, Pid, ToolContext


async def fetch_to_file(client: DataONEClient, url: str, path: Path, limit: int) -> tuple[int, bool]:
    """Stream a URL into ``path``, stopping after ``limit`` bytes. Returns (bytes written, cut short)."""
    total, cut = 0, False
    async with client.stream(url) as resp:
        with path.open("wb") as fh:
            async for chunk in resp.aiter_bytes():
                if total + len(chunk) > limit:
                    fh.write(chunk[: limit - total])
                    total, cut = limit, True
                    break
                fh.write(chunk)
                total += len(chunk)
    return total, cut


async def run_remote(client: DataONEClient, limits: ProfileSettings, func: Any, url: str, scan: str) -> dict[str, Any]:
    """Run a profiler that reads ``url`` itself over HTTP range requests, holding an upstream slot meanwhile."""
    try:
        async with client.limiter.slot():
            return await asyncio.wait_for(asyncio.to_thread(func, url, limits, scan), timeout=limits.timeout)
    except TimeoutError as exc:
        raise ToolError(
            f"Profiling took longer than {limits.timeout:.0f} seconds; try scan='sample' or a smaller file."
        ) from exc


async def profile_object(
    client: DataONEClient,
    limits: ProfileSettings,
    pid: str,
    scan: str = "sample",
    header_rows: int | str = "auto",
) -> dict[str, Any]:
    """Profile one data object. Shared by ``profile_file`` and ``compare_eml_vs_data``."""
    info = await downloads.object_info(client, pid)
    base = {k: info[k] for k in ("pid", "fileName", "formatId", "size")}
    fmt, name, url = info["formatId"], info["fileName"], info["download_url"]
    report = None
    if profiling_raster.is_raster(fmt, name):
        report = await run_remote(client, limits, profiling_raster.profile_raster, url, scan)
    elif profiling_netcdf.is_netcdf(fmt, name):
        if info["size"] and info["size"] <= limits.max_netcdf_download:
            # HDF5 reads in many small pieces, so over HTTP each one costs a round trip; one download is far faster.
            with tempfile.TemporaryDirectory(prefix="adc-profile-") as tmp:
                path = Path(tmp) / "data.nc"
                try:
                    await asyncio.wait_for(
                        fetch_to_file(client, url, path, limits.max_netcdf_download), limits.timeout
                    )
                except TimeoutError as exc:
                    raise ToolError(f"Downloading the file took longer than {limits.timeout:.0f} seconds.") from exc
                report = await profiling_netcdf.profile_in_subprocess(str(path), limits, scan, limits.timeout)
        else:
            async with client.limiter.slot():  # the child process reads the object URL itself
                report = await profiling_netcdf.profile_in_subprocess(url, limits, scan, limits.timeout)
    if report is not None:
        complete = report.pop("complete")
        return {**base, "supported": True, "scan": scan, "complete": complete, **report}
    if not profiling.is_delimited(fmt, name):
        return {
            **base,
            "supported": False,
            "reason": f"Cannot profile format {fmt!r} yet (archives such as zip and gzip included).",
            "supported_formats": [profiling.SUPPORTED, profiling_raster.SUPPORTED, profiling_netcdf.SUPPORTED],
            "download_url": url,
        }
    limit = limits.sample_bytes if scan == "sample" else limits.max_full_bytes
    with tempfile.TemporaryDirectory(prefix="adc-profile-") as tmp:
        path = Path(tmp) / "data"
        read, cut = await fetch_to_file(client, url, path, limit)
        if cut:
            profiling.trim_partial_line(path)
        report = await asyncio.to_thread(profiling.profile_delimited, path, limits, header_rows)
    rows = report.pop("rows_profiled")
    notes = report.pop("notes")
    scope = {"scan": scan, "complete": not cut, "bytes_read": read, "rows_profiled": rows}
    if cut:
        scope["estimated_total_rows"] = round(rows * info["size"] / read) if info["size"] and read else None
        notes.append(
            "Only the start of the file was read; row counts are estimates and min/max values, ranges and "
            "extents cover just that part."
            + (" Use scan='full' to read more." if scan == "sample" else " The file exceeds the server's limit.")
        )
    return {**base, "supported": True, "kind": "table", **scope, **report, "notes": notes}


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    client = ctx.client
    limits = ctx.settings.profile

    @mcp.tool(annotations=READ_ONLY, tags={"files", "metadata-quality"})
    async def profile_file(
        pid: Annotated[str, Field(description="PID of the data file (from list_dataset_files or search_data_files).")],
        scan: Annotated[
            Literal["sample", "full"],
            Field(description=(
                "'sample' reads only part of the file (fast): the start of a CSV, a few blocks of a raster, the start "
                "and end of each large NetCDF variable. 'full' reads everything up to the server's limits (see "
                "get_server_config)."
            )),
        ] = "sample",
        header_rows: Annotated[
            int | Literal["auto"],
            Field(ge=1, le=5, description=(
                "Header lines after any preamble: the first holds column names, the second (if present) units, "
                "and any further lines are skipped. 'auto' reads the second line as units when it looks like one. "
                "Set it yourself if the header was misread, e.g. 1 when the second line is actually data."
            )),
        ] = "auto",
    ) -> dict[str, Any]:
        """Summarize the contents of a data file so it can be compared with its EML. Supports delimited text
        (CSV, TSV), GeoTIFF rasters and NetCDF; other formats return supported=false.

        Delimited text: per-column type, missing values, ranges, example values, guessed units, and the extent
        inferred from latitude, longitude and date columns. Types, units and extents are guesses from the data and
        column names. Missing values are empty cells, NA/null/NaN and the sentinels -9999 and -99999 (counted
        separately as `sentinel_count`). Units come from a units line under the header when there is one.
        GeoTIFF: size, CRS, pixel size, a lat/lon bounding box, nodata and per-band statistics, all read from the
        file's own georeferencing; statistics come from sampled blocks unless `complete`.
        NetCDF: dimensions, global attributes, and per variable the dtype, units, long name, fill value and
        statistics, plus spatial and temporal extents from the coordinate variables (CF time units are decoded).
        GeoTIFF is read over HTTP range requests; NetCDF is downloaded when it is not huge, otherwise range-read.
        Check `complete` before trusting row counts, statistics and extents.
        Example: profile_file(pid="urn:uuid:...", scan="full")
        """
        return await profile_object(client, limits, pid, scan, header_rows)
