"""Profile GeoTIFF (and other TIFF) rasters without downloading them.

The file is opened over HTTP range requests, so only the header and the blocks that are read are fetched. In sample
mode a few blocks spread over the image give the band statistics, because a decimated read of a file without
overviews still has to fetch nearly all of it. Everything here is synchronous and meant to run in a worker thread.
Georeferencing, data types, nodata values and tags come from the file itself and are not guesses; band statistics
are exact only when ``complete`` is true.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from fastmcp.exceptions import ToolError
from rasterio.errors import RasterioError
from rasterio.warp import transform_bounds
from rasterio.windows import Window

from arcticdata_mcp.client import USER_AGENT
from arcticdata_mcp.config import ProfileSettings

RASTER_FORMATS = {"image/tiff", "image/geotiff"}
GENERIC_FORMATS = {None, "application/octet-stream"}
RASTER_EXTENSIONS = {".tif", ".tiff", ".gtif", ".geotiff"}
SUPPORTED = "GeoTIFF and other TIFF rasters"
MAX_BANDS = 50
MAX_DISTINCT = 20
MAX_TAGS = 20


def is_raster(format_id: str | None, file_name: str | None) -> bool:
    ext = Path(file_name).suffix.lower() if file_name else ""
    return format_id in RASTER_FORMATS or (format_id in GENERIC_FORMATS and ext in RASTER_EXTENSIONS)


def _finite(value: Any) -> float | None:
    return float(value) if value is not None and math.isfinite(float(value)) else None


class _BandStats:
    """Running statistics of one band over many windows."""

    def __init__(self, integer: bool):
        self.integer = integer
        self.n = self.masked = 0
        self.total = self.total_sq = 0.0
        self.lo, self.hi = math.inf, -math.inf
        self.distinct: set[float] | None = set() if integer else None

    def add(self, block: np.ma.MaskedArray) -> None:
        if np.issubdtype(block.dtype, np.floating):
            block = np.ma.masked_invalid(block)
        valid = block.compressed()
        self.masked += block.size - valid.size
        if not valid.size:
            return
        v = valid.astype("float64")
        self.n += v.size
        self.total += float(v.sum())
        self.total_sq += float((v * v).sum())
        self.lo, self.hi = min(self.lo, float(v.min())), max(self.hi, float(v.max()))
        if self.distinct is not None:
            self.distinct.update(np.unique(v).tolist())
            if len(self.distinct) > MAX_DISTINCT:
                self.distinct = None

    def summary(self) -> dict[str, Any]:
        seen = self.n + self.masked
        out: dict[str, Any] = {"valid_fraction": round(self.n / seen, 4) if seen else None}
        if self.n:
            mean = self.total / self.n
            out.update(
                min=self.lo,
                max=self.hi,
                mean=round(mean, 6),
                std=round(math.sqrt(max(0.0, self.total_sq / self.n - mean * mean)), 6),
            )
            if self.distinct:
                out["distinct_values"] = sorted(self.distinct)
        return out


def _windows(src: rasterio.DatasetReader, limits: ProfileSettings, full: bool) -> tuple[list[Window], int]:
    """Block-aligned windows to read, and how many such windows the image has."""
    block_h, block_w = src.block_shapes[0]
    rows = block_h * max(1, math.ceil(limits.sample_block_pixels / max(1, block_w * block_h)))
    n_y, n_x = math.ceil(src.height / rows), math.ceil(src.width / block_w)
    total = n_y * n_x
    picks = (
        range(total) if full else sorted({int(p) for p in np.linspace(0, total - 1, min(limits.sample_blocks, total))})
    )
    wins = []
    for p in picks:
        by, bx = divmod(p, n_x)
        wins.append(
            Window(bx * block_w, by * rows, min(block_w, src.width - bx * block_w), min(rows, src.height - by * rows))
        )
    return wins, total


def _crs_info(crs: Any) -> dict[str, Any]:
    name = re.match(r'^\w+\["([^"]+)"', crs.wkt or "")
    out: dict[str, Any] = {
        "epsg": crs.to_epsg(),
        "name": name.group(1) if name else None,
        "geographic": crs.is_geographic,
    }
    if not crs.is_geographic:
        out["linear_units"] = crs.linear_units
    if out["epsg"] is None:  # a custom CRS has no code, so show its definition
        out["proj"] = (crs.to_proj4() or crs.wkt)[:300]
    return out


def _tags(src: rasterio.DatasetReader) -> dict[str, str]:
    return {k: str(v)[:200] for k, v in list(src.tags().items())[:MAX_TAGS]}


def profile_raster(source: str, limits: ProfileSettings, scan: str) -> dict[str, Any]:
    """Profile a raster at a local path or an HTTP(S) URL."""
    env = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "GDAL_HTTP_USERAGENT": USER_AGENT,
        "GDAL_HTTP_MAX_RETRY": "3",
        "GDAL_HTTP_RETRY_DELAY": "1",
        "GDAL_HTTP_TIMEOUT": "30",
        "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
        "CPL_VSIL_CURL_CHUNK_SIZE": str(2 * 1024 * 1024),
    }
    try:
        with rasterio.Env(**env), rasterio.open(source) as src:
            return _profile(src, limits, scan)
    except RasterioError as exc:
        raise ToolError(f"Could not read the file as a raster: {str(exc)[:300]}") from exc


def _profile(src: rasterio.DatasetReader, limits: ProfileSettings, scan: str) -> dict[str, Any]:
    notes: list[str] = []
    n_bands = min(src.count, MAX_BANDS)
    indexes = list(range(1, n_bands + 1))
    full = scan == "full" and src.width * src.height * n_bands <= limits.max_full_pixels
    if scan == "full" and not full:
        notes.append(
            f"The raster has more than {limits.max_full_pixels:,} pixels (times bands), so it was sampled "
            "instead of read in full."
        )
    block_h, block_w = src.block_shapes[0]
    stats = [_BandStats(np.issubdtype(np.dtype(src.dtypes[i - 1]), np.integer)) for i in indexes]
    wins, total_windows = [], 0
    if block_w * block_h * n_bands > limits.max_full_pixels:
        notes.append("The file's blocks are too large to sample cheaply, so band statistics were skipped.")
    else:
        wins, total_windows = _windows(src, limits, full)
        for win in wins:
            data = src.read(indexes, window=win, masked=True)
            for band_stats, band in zip(stats, data):
                band_stats.add(band)
    complete = bool(wins) and len(wins) == total_windows and n_bands == src.count
    if wins and not complete:
        notes.append(
            f"Band statistics come from {len(wins)} of {total_windows} blocks spread over the image; "
            "min, max and mean are estimates and rare values may be missed."
        )

    bands = []
    for i, band_stats in zip(indexes, stats):
        nodata = src.nodatavals[i - 1]
        entry: dict[str, Any] = {
            "index": i,
            "dtype": src.dtypes[i - 1],
            "nodata": _finite(nodata) if nodata is None or math.isfinite(nodata) else "nan",
            "description": src.descriptions[i - 1],
            "unit": src.units[i - 1] or None,
            "color_interpretation": src.colorinterp[i - 1].name,
        }
        if src.scales[i - 1] not in (None, 1.0):
            entry["scale"] = src.scales[i - 1]
        if src.offsets[i - 1] not in (None, 0.0):
            entry["offset"] = src.offsets[i - 1]
        bands.append({**entry, **band_stats.summary()} if wins else entry)
    if src.count > n_bands:
        notes.append(f"Only the first {n_bands} of {src.count} bands were profiled.")

    spatial, crs = None, None
    if src.crs:
        crs = _crs_info(src.crs)
        try:
            west, south, east, north = transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=21)
            if all(math.isfinite(v) for v in (west, south, east, north)):
                spatial = {
                    "bbox": {"north": north, "south": south, "east": east, "west": west},
                    "crs": "EPSG:4326",
                    "source": "file georeferencing",
                }
        except Exception:  # an unusual CRS that PROJ can't transform is reported without a lat/lon box
            notes.append("The bounds could not be converted to latitude and longitude.")
    else:
        notes.append("The file has no coordinate reference system, so its location is unknown.")

    res_x, res_y = src.res
    left, bottom, right, top = src.bounds
    report: dict[str, Any] = {
        "kind": "raster",
        "driver": src.driver,
        "width": src.width,
        "height": src.height,
        "band_count": src.count,
        "crs": crs,
        "pixel_size": {"x": res_x, "y": res_y},
        "bounds_native": {"left": left, "bottom": bottom, "right": right, "top": top},
        "spatial_extent": spatial,
        "layout": {
            "tiled": bool(src.profile.get("tiled")),
            "block_size": [block_w, block_h],
            "compression": src.compression.name if src.compression else None,
            "overviews": src.overviews(1),
        },
        "tags": _tags(src),
        "bands": bands,
        "notes": notes,
        "complete": complete,
    }
    if wins:
        report["stats_basis"] = {"windows_read": len(wins), "windows_total": total_windows}
    return report
