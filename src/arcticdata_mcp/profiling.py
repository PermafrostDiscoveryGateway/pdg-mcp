"""Profile tabular data files so an agent can compare what a file contains with what its EML says.

Delimited text is parsed in chunks with pandas and summarized by per-column running statistics, so memory use
depends on the number of columns, not on the number of rows. Everything here is synchronous and meant to run in
a worker thread. Types, units and the spatial and temporal extents are inferred from the data and column names,
so they are guesses that the output labels as such.
"""

from __future__ import annotations

import csv
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastmcp.exceptions import ToolError

from arcticdata_mcp.config import ProfileSettings

HEAD_BYTES = 64 * 1024
DELIMITERS = (",", "\t", ";", "|")
DELIMITED_EXTENSIONS = {".csv", ".tsv", ".tab", ".txt", ".dat"}
DELIMITED_FORMATS = {"text/csv", "text/tsv", "text/tab-separated-values"}
# ADC labels many plain CSV files with a generic or spreadsheet format ID, so those count when the extension agrees.
GENERIC_FORMATS = {None, "text/plain", "application/octet-stream", "application/vnd.ms-excel"}
SUPPORTED = "delimited text (CSV, TSV and similar)"

NA_TOKENS = {"", "na", "n/a", "#n/a", "nan", "null", "none"}
BOOL_TOKENS = {"true", "false", "t", "f", "yes", "no"}
TYPE_FRACTION = 0.95  # share of non-missing values that must parse for a column to get that type
TIME_ONLY = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?(\.\d+)?\s*([AaPp][Mm])?$")
DATE_LIKE = re.compile(r"^\d{1,4}[-/]\d{1,2}[-/]\d{1,4}|^\d{4}-\d{2}-\d{2}T|^\d{1,2}:\d{2}")
DATE_NAME_TOKENS = {"date", "datetime", "time", "timestamp"}
YEAR_NAME_TOKENS = {"year", "yr", "yyyy"}
# Columns that say when a file or record was made, not when the data were collected.
RECORD_DATE_TOKENS = {
    "processing",
    "processed",
    "created",
    "creation",
    "modified",
    "updated",
    "generated",
    "uploaded",
    "exported",
    "downloaded",
    "retrieved",
    "accessed",
}
LAT_TOKENS = {"lat", "latitude"}
LON_TOKENS = {"lon", "long", "lng", "longitude"}
UNIT_IN_BRACKETS = re.compile(r"[(\[]([^)\]]{1,20})[)\]]\s*$")
UNIT_SUFFIXES = {
    "c",
    "degc",
    "k",
    "m",
    "cm",
    "mm",
    "km",
    "kg",
    "g",
    "mg",
    "ug",
    "l",
    "ml",
    "s",
    "ms",
    "min",
    "hr",
    "hz",
    "pct",
    "percent",
    "ppm",
    "ppt",
    "psu",
    "mps",
    "kpa",
    "pa",
    "hpa",
    "mbar",
    "ha",
    "deg",
    "degrees",
}


def is_delimited(format_id: str | None, file_name: str | None) -> bool:
    ext = Path(file_name).suffix.lower() if file_name else ""
    if format_id in DELIMITED_FORMATS:
        return True
    return format_id in GENERIC_FORMATS and ext in DELIMITED_EXTENSIONS


def trim_partial_line(path: Path) -> None:
    """Cut a truncated file back to its last complete line."""
    size = path.stat().st_size
    with path.open("r+b") as fh:
        pos = size
        while pos > 0:
            step = min(65536, pos)
            pos -= step
            fh.seek(pos)
            idx = fh.read(step).rfind(b"\n")
            if idx != -1:
                fh.truncate(pos + idx + 1)
                return
        fh.truncate(0)


def _tokens(name: str) -> list[str]:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]


def _match_tokens(name: str, exact: set[str], prefixes: tuple[str, ...] = ()) -> bool:
    return any(t in exact or t.startswith(prefixes) for t in _tokens(name))


def guess_unit(name: str) -> str | None:
    """Unit hinted by a column name: a trailing "(m)" or "[mm]", or a suffix such as "_C" or "_mm"."""
    if m := UNIT_IN_BRACKETS.search(name):
        return m.group(1).strip() or None
    tokens = _tokens(name)
    if len(tokens) > 1 and tokens[-1] in UNIT_SUFFIXES:
        return name.strip().rsplit("_", 1)[-1] if "_" in name else tokens[-1]
    return None


@dataclass
class _Layout:
    encoding: str
    delimiter: str
    skiprows: list[int]  # file lines pandas must skip: the preamble and any header lines after the column names
    preamble: int  # file lines before the header
    header_rows: int  # lines that make up the header: the column names, then (optionally) a units row
    units: list[str | None] | None  # one entry per column when the second header line is a units row


def _is_number(text: str) -> bool:
    try:
        float(text.replace(",", "."))  # decimal commas count as numbers
    except ValueError:
        return False
    return True


def _unit_cell(cell: str) -> str | None:
    text = cell.strip().strip("()[] ")
    if not text or text.lower() in NA_TOKENS or _is_number(text) or len(text) > 20:
        return None
    if not re.search(r"[A-Za-zµ°%]", text):
        return None
    return text


FORMAT_TOKEN = re.compile(r"yyyy|yy|mm|dd|hh|ss|[-/.: T]", re.IGNORECASE)
FORMAT_DIRECTIVES = {"yyyy": "%Y", "yy": "%y", "dd": "%d", "hh": "%H", "ss": "%S"}


def format_from_hint(text: str) -> str | None:
    """strptime format for a date or time pattern such as "mm/dd/yy hh:mm:ss", or None if ``text`` isn't one.

    A lone "mm" is millimetres, so at least two date or time fields are required. "mm" means minutes next to
    hours or seconds and months otherwise.
    """
    tokens = FORMAT_TOKEN.findall(text.strip())
    if "".join(tokens) != text.strip():
        return None
    fields = [i for i, t in enumerate(tokens) if t.lower() in (*FORMAT_DIRECTIVES, "mm")]
    if len(fields) < 2:
        return None
    out = list(tokens)
    for n, i in enumerate(fields):
        word = tokens[i].lower()
        if word != "mm":
            out[i] = FORMAT_DIRECTIVES[word]
            continue
        before = tokens[fields[n - 1]].lower() if n else ""
        after = tokens[fields[n + 1]].lower() if n + 1 < len(fields) else ""
        out[i] = "%M" if before == "hh" or after == "ss" else "%m"
    return "".join(out)


def _looks_like_units_row(rows: list[list[str]], ncols: int) -> bool:
    """Is rows[0] a units row: short non-numeric labels above columns whose remaining values are numbers?"""
    candidate, below = rows[0], rows[1:21]
    if len(below) < 2:
        return False
    numeric_cols = []
    for i in range(ncols):
        cells = [r[i].strip() for r in below if i < len(r) and r[i].strip().lower() not in NA_TOKENS]
        if cells and sum(_is_number(c) for c in cells) / len(cells) >= 0.8:
            numeric_cols.append(i)
    if not numeric_cols:
        return False
    labelled = sum(i < len(candidate) and _unit_cell(candidate[i]) is not None for i in numeric_cols)
    return labelled / len(numeric_cols) >= 0.6


def _detect_layout(head: bytes, full_head: bool, header_rows: int | str = "auto") -> _Layout:
    if b"\x00" in head[:4096]:
        raise ToolError("File looks binary, not delimited text.")
    probe = head[:-4] if full_head else head
    try:
        text, encoding = probe.decode("utf-8-sig"), "utf-8-sig"
    except UnicodeDecodeError:
        text, encoding = probe.decode("latin-1"), "latin-1"
    numbered = [(i, ln) for i, ln in enumerate(text.splitlines()) if ln.strip()]  # (file line, text)
    if full_head:
        numbered = numbered[:-1]  # the last line is cut off at the end of the head
    numbered = numbered[:200]
    if not numbered:
        raise ToolError("File is empty.")
    rows = {d: [next(csv.reader([ln], delimiter=d), []) for _, ln in numbered] for d in DELIMITERS}
    best, best_score, best_counts = ",", 0.0, [0] * len(numbered)
    for delim, parsed in rows.items():
        counts = [len(r) - 1 for r in parsed]
        mode, freq = Counter(counts).most_common(1)[0]
        score = mode * freq / len(numbered) if mode else 0.0
        if score > best_score:
            best, best_score, best_counts = delim, score, counts
    parsed = rows[best]
    mode = Counter(best_counts).most_common(1)[0][0]
    k = best_counts.index(mode) if mode else 0
    # A title row (one filled cell, the rest empty) above the header is preamble too.
    while mode and k + 1 < len(numbered) and sum(bool(c.strip()) for c in parsed[k]) <= 1:
        k += 1

    after = parsed[k + 1 :]
    if header_rows == "auto":
        n = 2 if after and _looks_like_units_row(after, len(parsed[k])) else 1
    else:
        n = max(1, min(int(header_rows), len(numbered) - k))
    units = [_unit_cell(c) for c in after[0]] if n >= 2 and after else None
    if units is not None and not any(units):
        units = None
    preamble = numbered[k][0]
    extra = [numbered[k + j][0] for j in range(1, n)]
    return _Layout(encoding, best, list(range(preamble)) + extra, preamble, n, units)


@dataclass
class _Column:
    name: str
    rows: int = 0
    nulls: int = 0
    sentinels: int = 0
    numeric: int = 0
    integers: int = 0
    whole: int = 0  # numeric values with no fractional part, however they are written ("41.0" counts)
    booleans: int = 0
    datetimes: int = 0
    num_min: float = math.inf
    num_max: float = -math.inf
    num_sum: float = 0.0
    dt_min: pd.Timestamp | None = None
    dt_max: pd.Timestamp | None = None
    dt_candidate: bool | None = None
    time_only: bool = False
    format_hint: str | None = None  # date pattern from a header row, e.g. "mm/dd/yy hh:mm:ss"
    dt_format: str | None = None  # the same as a strptime format
    t_min: int | None = None
    t_max: int | None = None
    distinct: dict[str, int] = field(default_factory=dict)
    distinct_capped: bool = False
    tracked_values: int = 0
    examples: list[str] = field(default_factory=list)

    @property
    def non_null(self) -> int:
        return self.rows - self.nulls - self.sentinels

    def type(self) -> str:
        n = self.non_null
        if n <= 0:
            return "empty"
        if self.numeric / n >= TYPE_FRACTION:
            return "integer" if self.integers == self.numeric else "float"
        if self.dt_candidate and self.datetimes / n >= TYPE_FRACTION:
            return "time" if self.time_only else "datetime"
        if self.booleans == n:
            return "boolean"
        return "string"


def _parse_dates(col: _Column, vals: pd.Series) -> pd.Series:
    """Parse with the format declared in the file when it fits (also with the other year width), else generically."""
    declared = col.dt_format
    attempts = (
        [declared, declared.replace("%y", "%Y") if "%y" in declared else declared.replace("%Y", "%y")]
        if declared
        else []
    )
    for fmt in [*attempts, "mixed"]:
        parsed = pd.to_datetime(vals, errors="coerce", format=fmt, utc=True)
        if parsed.notna().mean() >= TYPE_FRACTION:
            col.dt_format = None if fmt == "mixed" else fmt
            return parsed
    return parsed


def _update(col: _Column, raw: pd.Series, sentinels: list[float], limits: ProfileSettings) -> None:
    col.rows += len(raw)
    stripped = raw.str.strip()
    null = raw.isna() | stripped.str.lower().isin(NA_TOKENS)
    col.nulls += int(null.sum())
    vals = stripped[~null]
    if vals.empty:
        return
    if len(col.examples) < 3:
        col.examples += [str(v)[:80] for v in vals.head(3 - len(col.examples))]

    num = pd.to_numeric(vals, errors="coerce")
    sentinel = num.isin(sentinels) if sentinels else pd.Series(False, index=num.index)
    col.sentinels += int(sentinel.sum())
    vals, num = vals[~sentinel], num[~sentinel]
    if vals.empty:
        return

    finite = pd.Series(np.isfinite(num.to_numpy(dtype="float64", na_value=np.nan)), index=num.index)
    col.numeric += int(finite.sum())
    if finite.any():
        col.integers += int(vals[finite].str.fullmatch(r"[+-]?\d+").sum())
        nums = num[finite]
        col.whole += int((nums % 1 == 0).sum())
        col.num_min = min(col.num_min, float(nums.min()))
        col.num_max = max(col.num_max, float(nums.max()))
        col.num_sum += float(nums.sum())
    col.booleans += int(vals.str.lower().isin(BOOL_TOKENS).sum())

    if col.dt_candidate is None:
        sample = vals.head(50)
        looks_dated = sample.str.match(DATE_LIKE).mean() >= 0.8
        col.time_only = bool(sample.str.match(TIME_ONLY).mean() >= 0.8)
        col.dt_candidate = bool(looks_dated or col.time_only or _match_tokens(col.name, DATE_NAME_TOKENS))
    if col.dt_candidate:
        parsed = _parse_dates(col, vals).dropna()
        col.datetimes += len(parsed)
        if len(parsed) and col.time_only:
            secs = parsed.dt.hour * 3600 + parsed.dt.minute * 60 + parsed.dt.second
            lo, hi = int(secs.min()), int(secs.max())
            col.t_min = lo if col.t_min is None else min(col.t_min, lo)
            col.t_max = hi if col.t_max is None else max(col.t_max, hi)
        elif len(parsed):
            lo, hi = parsed.min(), parsed.max()
            col.dt_min = lo if col.dt_min is None else min(col.dt_min, lo)
            col.dt_max = hi if col.dt_max is None else max(col.dt_max, hi)

    if finite.sum() / len(vals) < TYPE_FRACTION and not col.dt_candidate:
        for value, count in vals.value_counts().items():
            if value in col.distinct:
                col.distinct[value] += int(count)
            elif len(col.distinct) < limits.max_distinct:
                col.distinct[value] = int(count)
            else:
                col.distinct_capped = True


def _iso(ts: pd.Timestamp | None) -> str | None:
    return ts.strftime("%Y-%m-%dT%H:%M:%S") if ts is not None else None


def _clock(seconds: int | None) -> str | None:
    return None if seconds is None else f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _summary(col: _Column, limits: ProfileSettings) -> dict[str, Any]:
    kind = col.type()
    out: dict[str, Any] = {"name": col.name, "type": kind, "non_null": col.non_null, "null_count": col.nulls}
    if col.sentinels:
        out["sentinel_count"] = col.sentinels
    if kind in ("integer", "float"):
        out.update(min=col.num_min, max=col.num_max, mean=round(col.num_sum / col.numeric, 6))
        if kind == "float" and col.whole == col.numeric:
            out["whole_numbers"] = True  # written with a decimal point but every value is a whole number
        if col.numeric < col.non_null:
            out["non_numeric_count"] = col.non_null - col.numeric
        if unit := guess_unit(col.name):
            out["unit_guess"] = unit
            out["unit_source"] = "column name"
    elif kind == "datetime":
        out.update(min=_iso(col.dt_min), max=_iso(col.dt_max))
    elif kind == "time":
        out.update(min=_clock(col.t_min), max=_clock(col.t_max))
    if kind in ("datetime", "time") and col.format_hint:
        out["datetime_format"] = col.format_hint
    elif kind == "string":
        out["distinct"] = len(col.distinct)
        if col.distinct_capped:
            out["distinct_is_lower_bound"] = True
        else:
            top = sorted(col.distinct.items(), key=lambda kv: (-kv[1], kv[0]))[: limits.top_values]
            out["top_values"] = [{"value": v[:80], "count": c} for v, c in top]
    if kind != "empty":
        out["examples"] = col.examples
    return out


def _spatial(columns: list[dict[str, Any]]) -> dict[str, Any] | None:
    def candidates(tokens: set[str], prefix: str, lo: float, hi: float) -> list[dict[str, Any]]:
        return [
            c
            for c in columns
            if c.get("type") in ("integer", "float")
            and _match_tokens(c["name"], tokens, (prefix,))
            and lo <= c["min"]
            and c["max"] <= hi
            and (not c.get("unit_guess") or c["unit_guess"].lower().startswith(("deg", "\N{DEGREE SIGN}")))
        ]

    lats = candidates(LAT_TOKENS, "latitude", -90, 90)
    lons = candidates(LON_TOKENS, "longitude", -180, 360)
    if not lats or not lons:
        return None
    lat, lon = lats[0], lons[0]
    out: dict[str, Any] = {
        "latitude_column": lat["name"],
        "longitude_column": lon["name"],
        "bbox": {"north": lat["max"], "south": lat["min"], "east": lon["max"], "west": lon["min"]},
        "note": "min/max of the two columns; antimeridian crossings are not handled",
    }
    if lon["max"] > 180:
        out["longitude_convention"] = "0-360"
    return out


def _temporal(columns: list[dict[str, Any]]) -> dict[str, Any] | None:
    found, ignored = [], []
    for c in columns:
        if c.get("type") == "datetime":
            entry = {"column": c["name"], "start": c["min"], "end": c["max"]}
        elif (
            c.get("type") == "integer"
            and _match_tokens(c["name"], YEAR_NAME_TOKENS)
            and 1800 <= c["min"]
            and c["max"] <= 2200
        ):
            entry = {"column": c["name"], "start": str(int(c["min"])), "end": str(int(c["max"]))}
        else:
            continue
        (ignored if _match_tokens(c["name"], RECORD_DATE_TOKENS) else found).append(entry)
    if not found:
        return None
    out: dict[str, Any] = {
        "columns": found,
        "start": min(f["start"] for f in found),
        "end": max(f["end"] for f in found),
        "note": "times are read as written; time zones are not interpreted",
    }
    if ignored:
        out["ignored_columns"] = [
            {**i, "reason": "name suggests when the record was made, not when the data were collected"} for i in ignored
        ]
    return out


def profile_delimited(path: Path, limits: ProfileSettings, header_rows: int | str = "auto") -> dict[str, Any]:
    """Profile a delimited text file on disk.

    ``header_rows`` is the number of header lines: the first holds the column names and the second, if any, units.
    "auto" reads the second line as units when it looks like one.
    """
    with path.open("rb") as fh:
        head = fh.read(HEAD_BYTES)
    layout = _detect_layout(head, full_head=len(head) == HEAD_BYTES, header_rows=header_rows)
    read = {
        "sep": layout.delimiter,
        "encoding": layout.encoding,
        "skiprows": layout.skiprows,
        "dtype": str,
        "keep_default_na": False,
    }
    try:
        names = [str(c).strip() for c in pd.read_csv(path, nrows=0, **read).columns]
        usecols = list(range(min(len(names), limits.max_columns)))
        sentinels = [float(s) for s in limits.sentinels if re.fullmatch(r"[+-]?\d+(\.\d+)?", s.strip())]
        columns = [_Column(names[i]) for i in usecols]
        for col, cell in zip(columns, layout.units or []):
            if cell and (fmt := format_from_hint(cell)):
                col.format_hint, col.dt_format, col.dt_candidate = cell, fmt, True
                col.time_only = "%d" not in fmt and "%m" not in fmt and "%Y" not in fmt and "%y" not in fmt
        total_rows = 0
        reader = pd.read_csv(path, usecols=usecols, chunksize=limits.chunk_rows, on_bad_lines="skip", **read)
        with reader:
            for chunk in reader:
                total_rows += len(chunk)
                for i, col in enumerate(columns):
                    _update(col, chunk.iloc[:, i], sentinels, limits)
    except (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeDecodeError, ValueError) as exc:
        raise ToolError(f"Could not parse the file as delimited text: {exc}") from exc

    summaries = [_summary(c, limits) for c in columns]
    notes = []
    if layout.units:
        for summary, unit in zip(summaries, layout.units):
            if unit and format_from_hint(unit) is None and summary["type"] != "empty":
                summary["unit_guess"], summary["unit_source"] = unit, "header row"
        notes.append("The second header line was read as units.")
    if sum(n.startswith("Unnamed:") for n in names) >= 3:
        notes.append("Several columns have no header name; the real header may be on a later row or span rows.")
    if len(names) > len(columns):
        notes.append(f"Only the first {len(columns)} of {len(names)} columns were profiled.")
        summaries += [{"name": n, "profiled": False} for n in names[len(columns) :]]
    return {
        "dialect": {
            "delimiter": layout.delimiter,
            "encoding": "utf-8" if layout.encoding == "utf-8-sig" else layout.encoding,
            "preamble_lines_skipped": layout.preamble,
            "header_rows": layout.header_rows,
        },
        "rows_profiled": total_rows,
        "columns_total": len(names),
        "columns": summaries,
        "inferred_spatial_extent": _spatial(summaries),
        "inferred_temporal_extent": _temporal(summaries),
        "notes": notes,
    }
