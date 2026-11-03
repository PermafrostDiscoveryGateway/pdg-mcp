"""Profile NetCDF files (classic and NetCDF-4).

A local path is read directly (the tool downloads files of moderate size first). An HTTP(S) URL is opened over range
requests (``#mode=bytes``), which suits huge files but is slow for NetCDF-4, whose layout needs many small reads.
Dimensions, variables, units, long names, fill values and global attributes are read from the file itself and
are not guesses. Variable statistics are exact only when ``complete`` is true: otherwise they come from the start and
the end of each large variable. Values are reported after netCDF4's automatic masking of fill values and scaling.
Reading runs in a short-lived subprocess (``profile_in_subprocess``): netCDF-C and HDF5 cannot be interrupted, so a
slow remote read would otherwise hold a thread, and the lock around the library, long after the tool timed out.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import netCDF4
import numpy as np
from fastmcp.exceptions import ToolError

from arcticdata_mcp.config import ProfileSettings
from arcticdata_mcp.profiling import LAT_TOKENS, LON_TOKENS, _match_tokens

NETCDF_FORMATS = {"netCDF-4", "netCDF-3", "netCDF"}
GENERIC_FORMATS = {None, "application/octet-stream"}
NETCDF_EXTENSIONS = {".nc", ".nc4", ".cdf", ".netcdf"}
SUPPORTED = "NetCDF (classic and NetCDF-4)"

GLOBAL_ATTRIBUTES = (
    "title", "summary", "abstract", "description", "institution", "source", "history", "references", "comment",
    "Conventions", "featureType", "project", "creator_name", "license", "keywords", "time_coverage_start",
    "time_coverage_end", "geospatial_lat_min", "geospatial_lat_max", "geospatial_lon_min", "geospatial_lon_max",
)
VARIABLE_ATTRIBUTES = (
    "units", "long_name", "standard_name", "calendar", "axis", "positive", "coordinates", "grid_mapping",
    "cell_methods", "valid_min", "valid_max", "valid_range", "scale_factor", "add_offset", "_FillValue",
    "missing_value", "flag_values", "flag_meanings",
)
LAT_UNITS = {"degrees_north", "degree_north", "degree_n", "degrees_n", "degreen", "degreesn"}
LON_UNITS = {"degrees_east", "degree_east", "degree_e", "degrees_e", "degreee", "degreese"}
COORDINATE_VALUES = 1_000_000  # values read of a latitude, longitude or time variable, whatever the scan
MAX_DIMENSIONS = 200
MAX_TEXT = 300

# netCDF-C and HDF5 are not thread-safe, and profiles run in worker threads.
_LOCK = threading.Lock()


def is_netcdf(format_id: str | None, file_name: str | None) -> bool:
    ext = Path(file_name).suffix.lower() if file_name else ""
    return format_id in NETCDF_FORMATS or (format_id in GENERIC_FORMATS and ext in NETCDF_EXTENSIONS)


def _json(value: Any) -> Any:
    """An attribute value as something JSON can carry."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + "…"
    if isinstance(value, np.ndarray):
        items = [_json(v) for v in value.ravel().tolist()[:20]]
        return items if value.size > 1 else (items[0] if items else None)
    if isinstance(value, np.generic):
        return _json(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return "nan" if math.isnan(value) else ("inf" if value > 0 else "-inf")
    return value


def _attributes(obj: Any, names: tuple[str, ...]) -> dict[str, Any]:
    have = set(obj.ncattrs())
    return {n: _json(obj.getncattr(n)) for n in names if n in have}


def _walk(group: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    for name, var in group.variables.items():
        yield f"{prefix}{name}", var
    for name, sub in group.groups.items():
        yield from _walk(sub, f"{prefix}{name}/")


def _dimensions(group: Any, prefix: str = "") -> list[dict[str, Any]]:
    out = [{"name": f"{prefix}{n}", "size": len(d), "unlimited": d.isunlimited()} for n, d in group.dimensions.items()]
    for name, sub in group.groups.items():
        out += _dimensions(sub, f"{prefix}{name}/")
    return out


def slabs(shape: tuple[int, ...], budget: int) -> tuple[list[tuple[Any, ...]], bool]:
    """Index tuples that together read at most about ``budget`` values, and whether they cover the whole variable.

    A variable that doesn't fit is read from its start and from its end, in whole trailing dimensions, because those
    are contiguous in the file and so cheap to fetch.
    """
    if math.prod(shape) <= budget:
        return [(Ellipsis,)], True
    half = max(1, budget // 2)
    trailing, k = 1, len(shape)
    while k > 0 and trailing * shape[k - 1] <= half:
        trailing *= shape[k - 1]
        k -= 1
    d = k - 1
    n = max(1, half // trailing)
    rest = (slice(None),) * (len(shape) - d - 1)
    return [(0,) * d + (slice(0, n),) + rest, (-1,) * d + (slice(-n, None),) + rest], False


def _role(name: str, var: Any) -> str | None:
    """latitude, longitude or time, from CF attributes first and the variable name second."""
    have = set(var.ncattrs())
    standard = str(var.getncattr("standard_name")).lower() if "standard_name" in have else ""
    units = str(var.getncattr("units")).lower().strip() if "units" in have else ""
    if standard == "latitude" or units in LAT_UNITS:
        return "latitude"
    if standard == "longitude" or units in LON_UNITS:
        return "longitude"
    if " since " in units or standard == "time":
        return "time"
    if units and not units.startswith(("deg", "\N{DEGREE SIGN}")):
        return None  # e.g. "down_long_hemisp" in W/m2 is longwave radiation, not a longitude
    leaf = name.rsplit("/", 1)[-1]
    if _match_tokens(leaf, LAT_TOKENS, ("latitude",)):
        return "latitude"
    if _match_tokens(leaf, LON_TOKENS, ("longitude",)):
        return "longitude"
    return None


def _read(var: Any, budget: int) -> tuple[dict[str, Any] | None, int, bool]:
    """Statistics of a numeric variable: (summary, values read, whole variable read)."""
    try:
        kind = np.dtype(var.dtype).kind
    except TypeError:
        return None, 0, True
    if kind not in "iuf" or any(s == 0 for s in var.shape):
        return None, 0, True
    parts, whole = slabs(tuple(var.shape), budget)
    read, values = 0, []
    for index in parts:
        block = np.ma.masked_invalid(np.ma.asarray(var[index]))
        read += block.size
        values.append(block.compressed().astype("float64"))
    valid = np.concatenate(values) if values else np.array([])
    out: dict[str, Any] = {"valid_count": int(valid.size), "basis": "all values" if whole else "start and end of the variable"}
    if valid.size:
        out.update(min=float(valid.min()), max=float(valid.max()), mean=round(float(valid.mean()), 6))
    return out, read, whole


def _decode_time(var: Any, lo: float, hi: float) -> tuple[str, str] | None:
    units = getattr(var, "units", "")
    if " since " not in str(units):
        return None
    try:
        dates = netCDF4.num2date([lo, hi], str(units), calendar=getattr(var, "calendar", "standard"))
        return dates[0].isoformat(), dates[1].isoformat()
    except Exception:  # an unusual calendar or units string: the raw values are still reported
        return None


def profile_netcdf(source: str, limits: ProfileSettings, scan: str) -> dict[str, Any]:
    """Profile a NetCDF file at a local path or an HTTP(S) URL."""
    target = source + "#mode=bytes" if source.startswith(("http://", "https://")) else source
    # A timed-out profile cannot be stopped and keeps the lock until it finishes, so don't queue behind it forever.
    if not _LOCK.acquire(timeout=limits.timeout):
        raise ToolError("Another NetCDF profile is still running on the server; try again shortly.")
    try:
        try:
            ds = netCDF4.Dataset(target, "r")
        except (OSError, RuntimeError) as exc:
            raise ToolError(f"Could not read the file as NetCDF: {str(exc)[:300]}") from exc
        try:
            return _profile(ds, limits, scan)
        finally:
            ds.close()
    finally:
        _LOCK.release()


def _profile(ds: Any, limits: ProfileSettings, scan: str) -> dict[str, Any]:
    notes: list[str] = []
    everything = list(_walk(ds))
    chosen = everything[: limits.max_columns]
    if len(everything) > len(chosen):
        notes.append(f"Only the first {len(chosen)} of {len(everything)} variables were profiled.")

    remaining = limits.max_full_values if scan == "full" else 0
    results: dict[str, tuple[dict[str, Any] | None, str | None]] = {}
    complete = len(everything) == len(chosen)
    # Small variables first, so a full scan spends its budget on as many whole variables as it can.
    for name, var in sorted(chosen, key=lambda nv: math.prod(nv[1].shape)):
        role = _role(name, var)
        budget = max(limits.sample_values, remaining)
        if role:
            budget = max(budget, COORDINATE_VALUES)
        try:
            stats, read, whole = _read(var, budget)
        except Exception as exc:  # one unreadable variable shouldn't lose the rest of the profile
            stats, read, whole = {"error": str(exc)[:120]}, 0, False
        remaining = max(0, remaining - read)
        complete &= whole
        results[name] = (stats, role)

    variables = []
    lats: list[tuple[str, dict[str, Any]]] = []
    lons: list[tuple[str, dict[str, Any]]] = []
    times: list[dict[str, Any]] = []
    for name, var in chosen:
        stats, role = results[name]
        entry: dict[str, Any] = {
            "name": name,
            "dtype": str(np.dtype(var.dtype)) if not isinstance(var.dtype, type) else "string",
            "dimensions": list(var.dimensions),
            "shape": list(var.shape),
            **_attributes(var, VARIABLE_ATTRIBUTES),
        }
        if role:
            entry["role"] = role
        if stats:
            entry["stats"] = stats
        variables.append(entry)
        if stats and "min" in stats:
            if role == "latitude" and -90 <= stats["min"] and stats["max"] <= 90:
                lats.append((name, stats))
            elif role == "longitude" and -180 <= stats["min"] and stats["max"] <= 360:
                lons.append((name, stats))
            elif role == "time":
                decoded = _decode_time(var, stats["min"], stats["max"])
                if decoded:
                    times.append({"variable": name, "start": decoded[0], "end": decoded[1]})
    variables += [{"name": n, "profiled": False} for n, _ in everything[len(chosen):]]

    spatial = None
    if lats and lons:
        (lat_name, lat), (lon_name, lon) = lats[0], lons[0]
        spatial = {
            "latitude_variable": lat_name,
            "longitude_variable": lon_name,
            "bbox": {"north": lat["max"], "south": lat["min"], "east": lon["max"], "west": lon["min"]},
            "source": "coordinate variables",
        }
        if lon["max"] > 180:
            spatial["longitude_convention"] = "0-360"
        if "start and end of the variable" in (lat["basis"], lon["basis"]):
            spatial["note"] = "from the start and end of the coordinate variables"
    temporal = None
    if times:
        temporal = {
            "variables": times,
            "start": min(t["start"] for t in times),
            "end": max(t["end"] for t in times),
            "source": "time variables with CF units",
        }

    if not complete:
        notes.append(
            "Variable statistics for large variables come from their start and end; min, max and mean are estimates. "
            + ("Use scan='full' to read more." if scan == "sample" else "The file exceeds the server's limit.")
        )
    return {
        "kind": "netcdf",
        "file_format": ds.data_model,
        "dimensions": _dimensions(ds)[:MAX_DIMENSIONS],
        "global_attributes": _attributes(ds, GLOBAL_ATTRIBUTES),
        "other_global_attributes": max(0, len(ds.ncattrs()) - len(_attributes(ds, GLOBAL_ATTRIBUTES))),
        "variables": variables,
        "spatial_extent": spatial,
        "temporal_extent": temporal,
        "notes": notes,
        "complete": complete,
    }


async def profile_in_subprocess(source: str, limits: ProfileSettings, scan: str, timeout: float) -> dict[str, Any]:
    """Run ``profile_netcdf`` in a child process that is killed if it outlives ``timeout`` seconds."""
    request = json.dumps({"source": source, "limits": dataclasses.asdict(limits), "scan": scan}).encode()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "arcticdata_mcp.profiling_netcdf",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(request), timeout)
    except BaseException as exc:  # timeout or cancellation: don't leave the child reading
        proc.kill()
        await proc.wait()
        if isinstance(exc, TimeoutError):
            raise ToolError(f"Profiling took longer than {timeout:.0f} seconds; try scan='sample' or a smaller file.") from exc
        raise
    try:
        reply = json.loads(out)
    except ValueError:
        raise ToolError("The NetCDF reader stopped unexpectedly (the file may be corrupt or too large).") from None
    if "error" in reply:
        raise ToolError(reply["error"])
    return reply["report"]


def _main() -> None:
    request = json.loads(sys.stdin.buffer.read())
    try:
        report = profile_netcdf(request["source"], ProfileSettings(**request["limits"]), request["scan"])
        reply = {"report": report}
    except ToolError as exc:
        reply = {"error": str(exc)}
    sys.stdout.write(json.dumps(reply, default=str))


if __name__ == "__main__":
    _main()
