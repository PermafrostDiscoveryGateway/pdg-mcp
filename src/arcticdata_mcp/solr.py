"""Helpers for building Solr queries against the DataONE index and normalizing results."""

from __future__ import annotations

import calendar
import re
from typing import Any

from fastmcp.exceptions import ToolError

from arcticdata_mcp.client import DataONEClient

MAX_ROWS = 100
ABSTRACT_CHARS = 400

METADATA_FIELDS = [
    "id", "seriesId", "title", "origin", "pubDate", "datePublished", "dateUploaded",
    "beginDate", "endDate", "northBoundCoord", "southBoundCoord", "eastBoundCoord",
    "westBoundCoord", "keywords", "abstract", "formatId", "resourceMap", "obsoletedBy",
]
DATA_FIELDS = [
    "id", "fileName", "formatId", "formatType", "size", "checksum", "checksumAlgorithm",
    "isDocumentedBy", "resourceMap", "dateUploaded", "obsoletedBy",
]

SORT_OPTIONS = {
    "relevance": "score desc",
    "newest": "dateUploaded desc",
    "oldest": "dateUploaded asc",
    "published_newest": "datePublished desc",
    "title": "titlestr asc",
    "size_desc": "size desc",
    "size_asc": "size asc",
}

_SPECIAL = re.compile(r'([+\-!(){}\[\]^"~:\\/]|&&|\|\|)')
_DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


def phrase(value: str) -> str:
    """Quote a value as an exact Solr phrase."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def term(value: str) -> str:
    """Build a Solr term from user input.

    Values containing ``*`` or ``?`` are treated as wildcard patterns (special
    characters and spaces escaped, wildcards kept); anything else becomes a phrase.
    """
    value = value.strip()
    if not value:
        raise ToolError("Search value must not be empty")
    if "*" in value or "?" in value:
        return _SPECIAL.sub(r"\\\1", value).replace(" ", "\\ ")
    return phrase(value)


def any_field(fields: list[str], value: str) -> str:
    """OR a single value across several fields."""
    t = term(value)
    return "(" + " OR ".join(f"{f}:{t}" for f in fields) + ")"


def solr_date(value: str | None, end_of_period: bool = False) -> str:
    """Convert YYYY / YYYY-MM / YYYY-MM-DD / ISO / Solr date-math into a Solr date. None -> '*'."""
    if value is None or value == "" or value == "*":
        return "*"
    value = value.strip()
    if value.upper().startswith("NOW"):
        return value.upper()
    if _DATE_RE.match(value):
        parts = value.split("-")
        if len(parts) == 1:
            return f"{parts[0]}-12-31T23:59:59Z" if end_of_period else f"{parts[0]}-01-01T00:00:00Z"
        if len(parts) == 2:
            if end_of_period:
                last = calendar.monthrange(int(parts[0]), int(parts[1]))[1]
                return f"{parts[0]}-{parts[1]}-{last:02d}T23:59:59Z"
            return f"{parts[0]}-{parts[1]}-01T00:00:00Z"
        return f"{value}T23:59:59Z" if end_of_period else f"{value}T00:00:00Z"
    if "T" in value:
        return value if value.endswith("Z") else value + "Z"
    raise ToolError(f"Unrecognized date '{value}'. Use YYYY, YYYY-MM, YYYY-MM-DD, ISO-8601, or Solr NOW-math.")


def date_range(field: str, start: str | None, end: str | None) -> str:
    return f"{field}:[{solr_date(start)} TO {solr_date(end, end_of_period=True)}]"


def bbox_clause(north: float, south: float, east: float, west: float, mode: str = "intersects") -> str:
    if not (-90 <= south <= north <= 90):
        raise ToolError("Require -90 <= south <= north <= 90")
    if not (-180 <= west <= 180 and -180 <= east <= 180):
        raise ToolError("Longitudes must be in [-180, 180]")
    if mode == "within":
        return (
            f"southBoundCoord:[{south} TO *] AND northBoundCoord:[* TO {north}] AND "
            f"westBoundCoord:[{west} TO *] AND eastBoundCoord:[* TO {east}]"
        )
    return (
        f"southBoundCoord:[* TO {north}] AND northBoundCoord:[{south} TO *] AND "
        f"westBoundCoord:[* TO {east}] AND eastBoundCoord:[{west} TO *]"
    )


def _first(v: Any) -> Any:
    return v[0] if isinstance(v, list) and v else v


def summarize(doc: dict[str, Any], client: DataONEClient) -> dict[str, Any]:
    """Compact, agent-friendly view of a Solr document."""
    pid = doc.get("id")
    if doc.get("formatType") == "DATA" or ("fileName" in doc and "title" not in doc):
        out = {k: doc[k] for k in DATA_FIELDS if k in doc}
        out["download_url"] = client.object_url(pid)
        return out
    out: dict[str, Any] = {"id": pid, "title": doc.get("title")}
    if "seriesId" in doc:
        out["seriesId"] = doc["seriesId"]
    if "origin" in doc:
        out["authors"] = doc["origin"]
    for k in ("datePublished", "pubDate", "dateUploaded"):
        if k in doc:
            out["published"] = doc[k]
            break
    if "beginDate" in doc or "endDate" in doc:
        out["temporal"] = {"begin": doc.get("beginDate"), "end": doc.get("endDate")}
    if "northBoundCoord" in doc:
        out["bbox"] = {
            k: _first(doc.get(f"{k}BoundCoord")) for k in ("north", "south", "east", "west")
        }
    if "keywords" in doc:
        out["keywords"] = doc["keywords"][:15]
    if "abstract" in doc:
        a = doc["abstract"]
        out["abstract"] = a if len(a) <= ABSTRACT_CHARS else a[:ABSTRACT_CHARS] + "…"
    if "resourceMap" in doc:
        out["resourceMap"] = doc["resourceMap"]
    if doc.get("obsoletedBy"):
        out["obsoletedBy"] = doc["obsoletedBy"]
    out["landing_url"] = client.landing_url(pid)
    return out


async def search(
    client: DataONEClient,
    q: str | list[str],
    *,
    rows: int = 10,
    start: int = 0,
    sort: str = "relevance",
    format_type: str | None = "METADATA",
    latest_only: bool = True,
    fl: list[str] | None = None,
    extra_fq: list[str] | None = None,
    raw: bool = False,
) -> dict[str, Any]:
    """Run a search and return ``{numFound, start, rows, query, results}``."""
    clauses = [q] if isinstance(q, str) else [c for c in q if c]
    query = " AND ".join(f"({c})" for c in clauses) if clauses else "*:*"
    fq = list(extra_fq or [])
    if format_type:
        fq.append(f"formatType:{format_type}")
    if latest_only:
        fq.append("-obsoletedBy:*")
    if fl is None:
        fl = DATA_FIELDS if format_type == "DATA" else METADATA_FIELDS
    rows = max(0, min(rows, MAX_ROWS))
    params: list[tuple[str, Any]] = [
        ("q", query),
        ("fl", ",".join(fl)),
        ("rows", rows),
        ("start", max(0, start)),
        ("sort", SORT_OPTIONS.get(sort, sort)),
    ]
    params += [("fq", f) for f in fq]
    data = await client.solr(params)
    resp = data.get("response", {})
    docs = resp.get("docs", [])
    return {
        "numFound": resp.get("numFound", 0),
        "start": resp.get("start", 0),
        "rows": len(docs),
        "query": {"q": query, "fq": fq},
        "results": docs if raw else [summarize(d, client) for d in docs],
    }
