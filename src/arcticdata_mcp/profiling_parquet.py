"""Profile Parquet (and GeoParquet) files without downloading them.

A Parquet file ends with a footer that holds the schema, the row count and, for most columns, the minimum, maximum
and null count of every row group. That footer is read over HTTP range requests, so those facts are exact for the
whole file however large it is. Column data are then read for the things the footer lacks (mean, distinct values,
examples): a few column chunks within a byte budget in sample mode, everything up to the server's limit in full
mode. Geometry columns are not read; their extent comes from the GeoParquet metadata. Everything here is
synchronous and meant to run in a worker thread.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from fastmcp.exceptions import ToolError
from rasterio.crs import CRS
from rasterio.warp import transform_bounds

from arcticdata_mcp.client import USER_AGENT
from arcticdata_mcp.config import ProfileSettings
from arcticdata_mcp.profiling import DATE_LIKE, TYPE_FRACTION, _iso, _spatial, _temporal, guess_unit

PARQUET_FORMATS = {"application/vnd.apache.parquet", "application/parquet", "application/x-parquet"}
GENERIC_FORMATS = {None, "application/octet-stream"}
PARQUET_EXTENSIONS = {".parquet", ".parq", ".pq", ".geoparquet"}
SUPPORTED = "Parquet and GeoParquet"
READ_AHEAD = 256 * 1024  # smallest range requested, so the many small reads around the footer share one request
CACHED_BLOCKS = 8
SMALL_FILE = 1024 * 1024  # files up to this size are fetched whole
SAMPLE_ROW_GROUPS = 4  # row groups a sample draws from, spread over the file
MAX_KEY_LENGTH = 200  # distinct text values are tracked by their first characters
MAX_METADATA_ITEMS = 20
HIDDEN_METADATA = {"ARROW:schema", "pandas", "geo"}  # bulky and already reflected in the report


def is_parquet(format_id: str | None, file_name: str | None) -> bool:
    ext = Path(file_name).suffix.lower() if file_name else ""
    return format_id in PARQUET_FORMATS or (format_id in GENERIC_FORMATS and ext in PARQUET_EXTENSIONS)


class _Deadline:
    def __init__(self, seconds: float):
        self.seconds = seconds
        self.end = time.monotonic() + seconds

    def check(self) -> None:
        if time.monotonic() > self.end:
            raise ToolError(
                f"Profiling took longer than {self.seconds:.0f} seconds; try scan='sample' or a smaller file."
            )


class _RangeFile:
    """A read-only, seekable file over HTTP range requests, for pyarrow to read a remote Parquet file."""

    def __init__(self, url: str, size: int | None, deadline: _Deadline):
        self.url, self.deadline = url, deadline
        self.http = httpx.Client(timeout=30.0, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        self.pos = 0
        self.closed = False
        self.blocks: list[tuple[int, bytes]] = []
        self.bytes_fetched = self.requests = 0
        self.size = size if size else self._head_size()

    def _head_size(self) -> int:
        resp = self.http.head(self.url)
        length = resp.headers.get("content-length")
        if resp.status_code >= 400 or not length:
            self.http.close()
            raise ToolError("Could not find the size of the file, which is needed to read it in parts.")
        return int(length)

    def _fetch(self, start: int, length: int) -> bytes:
        end = min(self.size, start + length) - 1
        for attempt in range(3):
            self.deadline.check()
            try:
                resp = self.http.get(self.url, headers={"Range": f"bytes={start}-{end}"})
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise ToolError(f"Reading the file failed: {type(exc).__name__}") from exc
                time.sleep(1 + attempt)
                continue
            if resp.status_code == 206:
                self.requests += 1
                self.bytes_fetched += len(resp.content)
                return resp.content
            if resp.status_code == 200:
                raise ToolError("The server does not support range requests, so the file cannot be read in parts.")
            if resp.status_code < 500 and resp.status_code != 429:
                raise ToolError(f"Reading the file failed with HTTP {resp.status_code}.")
            time.sleep(1 + attempt)
        raise ToolError(f"Reading the file failed with HTTP {resp.status_code}.")

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        if n <= 0:
            return b""
        for start, data in self.blocks:
            if start <= self.pos and self.pos + n <= start + len(data):
                out = data[self.pos - start : self.pos - start + n]
                break
        else:
            if self.size <= SMALL_FILE:  # one request for the whole file beats several overlapping ones
                self.blocks = [(0, self._fetch(0, self.size))]
                return self.read(n)
            data = self._fetch(self.pos, max(n, READ_AHEAD))
            if n <= READ_AHEAD:  # big reads are column data, which are read once and not worth keeping
                self.blocks = [*self.blocks[-(CACHED_BLOCKS - 1) :], (self.pos, data)]
            out = data[:n]
        self.pos += len(out)
        return out

    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset
        return self.pos

    def tell(self) -> int:
        return self.pos

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.http.close()


def _kind(dtype: pa.DataType) -> str:
    """The report's column type for an Arrow type."""
    if pa.types.is_dictionary(dtype):
        dtype = dtype.value_type
    if pa.types.is_boolean(dtype):
        return "boolean"
    if pa.types.is_integer(dtype):
        return "integer"
    if pa.types.is_floating(dtype) or pa.types.is_decimal(dtype):
        return "float"
    if pa.types.is_timestamp(dtype) or pa.types.is_date(dtype):
        return "datetime"
    if pa.types.is_string(dtype) or pa.types.is_large_string(dtype) or pa.types.is_string_view(dtype):
        return "string"
    if pa.types.is_nested(dtype):
        return "nested"
    if pa.types.is_binary(dtype) or pa.types.is_large_binary(dtype) or pa.types.is_fixed_size_binary(dtype):
        return "binary"
    return "other"


READABLE = {"boolean", "integer", "float", "datetime", "string"}


def _timestamp(value: Any) -> pd.Timestamp | None:
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return None if ts is pd.NaT else ts


class _Column:
    """One top-level column: what the footer says about it and running statistics of the rows read."""

    def __init__(self, name: str, dtype: pa.DataType, leaf: int | None):
        self.name, self.dtype, self.leaf = name, dtype, leaf
        self.kind = _kind(dtype)
        # From the footer, over every row group; None when some row group lacks the statistic.
        self.footer_nulls: int | None = 0
        self.footer_min: Any = None
        self.footer_max: Any = None
        self.footer_range = leaf is not None and self.kind in ("integer", "float", "datetime")
        self.chunks_total = self.chunks_read = 0
        # From the rows read.
        self.rows = self.nulls = self.sentinels = self.nans = self.trues = 0
        self.count = 0  # values behind lo, hi and total
        self.lo: Any = None
        self.hi: Any = None
        self.total = 0.0
        self.whole = True
        self.dated: bool | None = None  # text column whose values look like dates; decided on the first rows
        self.dates = 0
        self.distinct: dict[str, int] = {}
        self.distinct_capped = False
        self.examples: list[str] = []

    def add_footer(self, stats: Any) -> None:
        self.chunks_total += 1
        if stats is None or not stats.has_null_count:
            self.footer_nulls = None
        elif self.footer_nulls is not None:
            self.footer_nulls += stats.null_count
        if not self.footer_range:
            return
        if stats is None or not stats.has_min_max:
            # A row group that is entirely null has no minimum or maximum, and that is not a gap.
            if stats is None or stats.num_values > 0:
                self.footer_range = False
            return
        lo, hi = stats.min, stats.max
        if isinstance(lo, float) and (math.isnan(lo) or math.isnan(hi)):
            self.footer_range = False
            return
        self.footer_min = lo if self.footer_min is None else min(self.footer_min, lo)
        self.footer_max = hi if self.footer_max is None else max(self.footer_max, hi)

    def add(self, arr: pa.Array, sentinels: list[float], limits: ProfileSettings) -> None:
        self.rows += len(arr)
        self.nulls += arr.null_count
        if pa.types.is_dictionary(arr.type):
            arr = arr.cast(arr.type.value_type)
        vals = arr.drop_null()
        if not len(vals):
            return
        if len(self.examples) < 3:
            self.examples += [str(v)[:80] for v in vals.slice(0, 3 - len(self.examples)).to_pylist()]
        if self.kind == "boolean":
            self.trues += pc.sum(vals).as_py() or 0
        elif self.kind in ("integer", "float"):
            self._add_numbers(vals, sentinels)
        elif self.kind == "datetime":
            bounds = pc.min_max(vals).as_py()
            lo, hi = _timestamp(bounds["min"]), _timestamp(bounds["max"])
            if lo is not None and hi is not None:
                self.lo = lo if self.lo is None else min(self.lo, lo)
                self.hi = hi if self.hi is None else max(self.hi, hi)
                self.count += len(vals)
        elif self.kind == "string":
            counts = pc.value_counts(vals).to_pylist()
            if self.dated is None:
                head = pd.Series([c["values"] for c in counts[:50]], dtype="object")
                self.dated = bool(head.str.match(DATE_LIKE).mean() >= 0.8)
            if self.dated:
                self._add_date_text(counts)
                return
            for item in counts:
                key = item["values"][:MAX_KEY_LENGTH]
                if key in self.distinct:
                    self.distinct[key] += item["counts"]
                elif len(self.distinct) < limits.max_distinct:
                    self.distinct[key] = item["counts"]
                else:
                    self.distinct_capped = True

    def _add_date_text(self, counts: list[dict[str, Any]]) -> None:
        """Dates stored as text: parse each distinct value once and keep the earliest and latest."""
        parsed = pd.to_datetime(
            pd.Series([c["values"] for c in counts], dtype="object"), errors="coerce", format="mixed", utc=True
        )
        ok = parsed.notna().to_numpy()
        self.dates += sum(c["counts"] for c, good in zip(counts, ok) if good)
        if ok.any():
            lo, hi = parsed.min(), parsed.max()
            self.lo = lo if self.lo is None else min(self.lo, lo)
            self.hi = hi if self.hi is None else max(self.hi, hi)

    def _add_numbers(self, vals: pa.Array, sentinels: list[float]) -> None:
        exact = vals.to_numpy(zero_copy_only=False) if self.kind == "integer" else None
        nums = pc.cast(vals, pa.float64(), safe=False).to_numpy(zero_copy_only=False)
        keep = np.isfinite(nums)
        self.nans += int((~keep).sum())
        if sentinels:
            missing = np.isin(nums, sentinels)
            self.sentinels += int(missing.sum())
            keep &= ~missing
        nums = nums[keep]
        if not nums.size:
            return
        # Integers keep their exact minimum and maximum, which a float cannot hold above 2**53.
        lo, hi = (int(exact[keep].min()), int(exact[keep].max())) if exact is not None else (nums.min(), nums.max())
        self.lo = lo if self.lo is None else min(self.lo, lo)
        self.hi = hi if self.hi is None else max(self.hi, hi)
        self.count += int(nums.size)
        self.total += float(nums.sum())
        self.whole = self.whole and bool((nums % 1 == 0).all())

    def _plain(self, value: Any) -> Any:
        if self.kind == "datetime":
            return _iso(_timestamp(value))
        return int(value) if self.kind == "integer" else float(value)

    def summary(self, rows_total: int, sentinels: list[float], limits: ProfileSettings) -> dict[str, Any]:
        kind = self.kind
        if kind == "string" and self.dated and self.dates >= TYPE_FRACTION * (self.rows - self.nulls):
            kind = "datetime"  # stored as text; arrow_type shows that
        out: dict[str, Any] = {"name": self.name, "type": kind, "arrow_type": str(self.dtype)}
        read_all = self.chunks_total > 0 and self.chunks_read == self.chunks_total
        if read_all:
            out.update(non_null=self.rows - self.nulls, null_count=self.nulls)
        elif self.footer_nulls is not None and self.leaf is not None:
            out.update(non_null=rows_total - self.footer_nulls, null_count=self.footer_nulls)
        if self.kind not in READABLE:
            return out
        out["rows_read"] = self.rows
        if self.sentinels:
            out["sentinel_count"] = self.sentinels
        if self.nans:
            out["nan_count"] = self.nans
        if self.kind in ("integer", "float", "datetime"):
            # Rows read in full give the range without missing-value codes; otherwise the footer covers every row.
            if not read_all and self.footer_range and self.footer_min is not None:
                out.update(min=self._plain(self.footer_min), max=self._plain(self.footer_max), range_from="footer")
                if self.kind != "datetime" and {out["min"], out["max"]} & set(sentinels):
                    out["range_note"] = "the footer's range includes a missing-value code"
            elif self.count:
                out.update(min=self._plain(self.lo), max=self._plain(self.hi), range_from="rows read")
            if out.get("min") is None:
                out.pop("min", None), out.pop("max", None), out.pop("range_from", None)
        if self.kind in ("integer", "float"):
            if self.count:
                out["mean"] = round(self.total / self.count, 6)
                if self.kind == "float" and self.whole:
                    out["whole_numbers"] = True
            if "min" in out and (unit := guess_unit(self.name)):
                out["unit_guess"], out["unit_source"] = unit, "column name"
        elif self.kind == "boolean" and self.rows > self.nulls:
            out["true_count"] = self.trues
        elif kind == "datetime" and self.kind == "string":
            out.update(min=_iso(self.lo), max=_iso(self.hi), range_from="rows read")
        elif self.kind == "string" and self.rows and not self.dated:
            out["distinct"] = len(self.distinct)
            if self.distinct_capped:
                out["distinct_is_lower_bound"] = True
            else:
                top = sorted(self.distinct.items(), key=lambda kv: (-kv[1], kv[0]))[: limits.top_values]
                out["top_values"] = [{"value": v[:80], "count": c} for v, c in top]
        if self.examples:
            out["examples"] = self.examples
        return out


def _crs(definition: Any) -> tuple[CRS | None, dict[str, Any]]:
    """The CRS of a GeoParquet geometry column; a missing one means longitude and latitude (OGC:CRS84)."""
    if definition is None:
        return CRS.from_epsg(4326), {"name": "WGS 84 (CRS84)", "epsg": 4326, "geographic": True}
    info: dict[str, Any] = {}
    crs = None
    if isinstance(definition, dict):
        info["name"] = definition.get("name")
        ident = definition.get("id") or {}
        if isinstance(ident, dict) and ident.get("authority") == "EPSG":
            info["epsg"] = ident.get("code")
    for candidate in (json.dumps(definition) if isinstance(definition, dict) else str(definition), info.get("epsg")):
        try:
            crs = CRS.from_epsg(candidate) if isinstance(candidate, int) else CRS.from_user_input(candidate)
        except Exception:  # try the EPSG code next; a CRS that stays unreadable is reported by name only
            continue
        info.setdefault("epsg", crs.to_epsg())
        info["geographic"] = crs.is_geographic
        break
    return crs, info


def _geometry(metadata: dict[bytes, bytes], notes: list[str]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The geometry columns described by GeoParquet metadata, and the latitude/longitude box of the primary one."""
    try:
        geo = json.loads(metadata[b"geo"])
        described = geo["columns"]
    except (KeyError, ValueError, TypeError):
        return None, None
    columns, extent, failed = [], None, False
    for name, col in described.items():
        crs, crs_info = _crs(col.get("crs"))
        entry: dict[str, Any] = {
            "name": name,
            "encoding": col.get("encoding"),
            "geometry_types": col.get("geometry_types"),
            "crs": crs_info,
        }
        bbox = col.get("bbox")
        if isinstance(bbox, list) and len(bbox) in (4, 6):
            half = len(bbox) // 2
            west, south, east, north = bbox[0], bbox[1], bbox[half], bbox[half + 1]
            entry["bbox_native"] = {"west": west, "south": south, "east": east, "north": north}
            if name == geo.get("primary_column") and crs is not None:
                try:
                    if not crs.is_geographic:
                        west, south, east, north = transform_bounds(
                            crs, "EPSG:4326", west, south, east, north, densify_pts=21
                        )
                    if all(math.isfinite(v) for v in (west, south, east, north)):
                        extent = {
                            "bbox": {"north": north, "south": south, "east": east, "west": west},
                            "crs": "EPSG:4326",
                            "source": f"GeoParquet metadata of column {name!r}",
                        }
                except Exception:
                    failed = True
                    notes.append("The geometry bounds could not be converted to latitude and longitude.")
        columns.append(entry)
    if columns and extent is None and not failed:
        notes.append("The GeoParquet metadata has no bounding box, so the spatial extent is unknown.")
    return {"primary_column": geo.get("primary_column"), "version": geo.get("version"), "columns": columns}, extent


def _plan(
    md: pq.FileMetaData, columns: list[_Column], budget: int, full: bool
) -> tuple[dict[int, list[_Column]], bool]:
    """Column chunks to read, by row group, within ``budget`` compressed bytes. Also says if any were left out.

    A file that does not fit the budget is read from row groups spread over its length, not from its start, so
    that the statistics are not biased towards the first rows. Within a row group the smallest chunks go first,
    so that as many columns as possible get some data.
    """
    readable = [c for c in columns if c.kind in READABLE and c.leaf is not None]
    n = md.num_row_groups
    if not n or not readable:
        return {}, False

    def sizes(g: int) -> list[tuple[int, _Column]]:
        rg = md.row_group(g)
        return sorted(((rg.column(c.leaf).total_compressed_size, c) for c in readable), key=lambda pair: pair[0])

    typical = sum(size for size, _ in sizes(0)) or 1
    if full and typical * n <= budget:
        picks = n
    else:
        picks = min(n, max(1, budget // typical) if full else SAMPLE_ROW_GROUPS)
    groups = sorted({int(g) for g in np.linspace(0, n - 1, picks)})
    plan: dict[int, list[_Column]] = {}
    spent, skipped = 0, False
    for g in groups:
        for size, col in sizes(g):
            if spent + size > budget:
                skipped = True
                continue
            spent += size
            plan.setdefault(g, []).append(col)
    return plan, skipped or len(groups) < n


def profile_parquet(source: str, limits: ProfileSettings, scan: str, size: int | None = None) -> dict[str, Any]:
    """Profile a Parquet file at a local path or an HTTP(S) URL. ``size`` is the remote file's size, if known."""
    deadline = _Deadline(limits.timeout)
    remote = source.startswith(("http://", "https://"))
    handle = _RangeFile(source, size, deadline) if remote else None
    try:
        with pq.ParquetFile(pa.PythonFile(handle, mode="r") if handle else source) as pf:
            report = _profile(pf, limits, scan, deadline)
    except (pa.ArrowException, OSError) as exc:
        raise ToolError(f"Could not read the file as Parquet: {str(exc)[:300]}") from exc
    finally:
        if handle:
            handle.close()
    if handle:
        report["read"] = {"bytes_fetched": handle.bytes_fetched, "requests": handle.requests, "file_size": handle.size}
    return report


def _profile(pf: pq.ParquetFile, limits: ProfileSettings, scan: str, deadline: _Deadline) -> dict[str, Any]:
    md, schema = pf.metadata, pf.schema_arrow
    notes: list[str] = []
    leaves = {md.schema.column(i).path: i for i in range(md.num_columns)}
    metadata = md.metadata or {}
    geometry, extent = _geometry(metadata, notes)
    geometry_names = {c["name"] for c in geometry["columns"]} if geometry else set()

    columns = [_Column(f.name, f.type, leaves.get(f.name)) for f in schema][: limits.max_columns]
    for col in columns:
        if col.name in geometry_names:
            col.kind = "geometry"
            col.footer_range = False
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        for col in columns:
            if col.leaf is not None:
                col.add_footer(rg.column(col.leaf).statistics)

    full = scan == "full"
    plan, partial = _plan(md, columns, limits.max_full_bytes if full else limits.sample_bytes, full)
    sentinels = [float(s) for s in limits.sentinels if _is_number(s)]
    for g, cols in plan.items():
        deadline.check()
        for batch in pf.iter_batches(batch_size=limits.chunk_rows, row_groups=[g], columns=[c.name for c in cols]):
            deadline.check()
            for col in cols:
                col.add(batch.column(col.name), sentinels, limits)
        for col in cols:
            col.chunks_read += 1

    summaries = [c.summary(md.num_rows, sentinels, limits) for c in columns]
    ranged = [s for s in summaries if "min" in s]  # only columns with a known range can give an extent
    complete = not partial and len(columns) == len(schema)
    rows_read = max((c.rows for c in columns), default=0)
    if partial:
        notes.append(
            "Row counts, null counts and the min/max marked range_from='footer' come from the file's footer and "
            f"cover every row. Means, distinct values and examples come from the rows read (at most {rows_read:,} "
            f"of {md.num_rows:,} per column)."
            + (" Use scan='full' to read more." if not full else " The file exceeds the server's limit.")
        )
    if any(c.kind in READABLE and c.leaf is not None and not c.chunks_read for c in columns):
        notes.append("Some columns have chunks larger than the read budget, so only their footer facts are given.")
    if geometry_names:
        notes.append("Geometry columns were not read; their extent comes from the GeoParquet metadata.")
    if len(schema) > len(columns):
        notes.append(f"Only the first {len(columns)} of {len(schema)} columns were profiled.")
        summaries += [{"name": f.name, "profiled": False} for f in list(schema)[len(columns) :]]

    codecs = sorted(
        {md.row_group(g).column(i).compression for g in range(min(md.num_row_groups, 1)) for i in range(md.num_columns)}
    )
    extra = {
        k.decode("utf-8", "replace"): v.decode("utf-8", "replace")[:200]
        for k, v in list(metadata.items())
        if k.decode("utf-8", "replace") not in HIDDEN_METADATA
    }
    return {
        "kind": "table",
        "format": "geoparquet" if geometry else "parquet",
        "rows_total": md.num_rows,
        "rows_profiled": rows_read,
        "row_groups": md.num_row_groups,
        "columns_total": len(schema),
        "columns": summaries,
        "geometry": geometry,
        "inferred_spatial_extent": extent or _spatial(ranged),
        "inferred_temporal_extent": _temporal(ranged),
        "file_metadata": {
            "created_by": md.created_by,
            "compression": codecs,
            "key_value": dict(list(extra.items())[:MAX_METADATA_ITEMS]),
        },
        "notes": notes,
        "complete": complete,
    }


def _is_number(text: str) -> bool:
    try:
        return math.isfinite(float(text))
    except ValueError:
        return False
