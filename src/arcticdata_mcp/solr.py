"""Helpers for building Solr queries against the DataONE index and normalizing results."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from typing import Any

from fastmcp.exceptions import ToolError

from arcticdata_mcp.client import DataONEClient, as_list

MAX_ROWS = 100
ABSTRACT_CHARS = 400

METADATA_FIELDS = [
    "id",
    "seriesId",
    "title",
    "origin",
    "pubDate",
    "datePublished",
    "dateUploaded",
    "beginDate",
    "endDate",
    "northBoundCoord",
    "southBoundCoord",
    "eastBoundCoord",
    "westBoundCoord",
    "keywords",
    "abstract",
    "formatId",
    "resourceMap",
    "obsoletedBy",
]
DATA_FIELDS = [
    "id",
    "fileName",
    "formatId",
    "formatType",
    "size",
    "checksum",
    "checksumAlgorithm",
    "isDocumentedBy",
    "resourceMap",
    "dateUploaded",
    "obsoletedBy",
]

# EML data-table attribute fields, for free-text variable searches.
ATTRIBUTE_FIELDS = ["attributeName", "attributeLabel", "attributeDescription", "attributeUnit", "attribute"]

SORT_OPTIONS = {
    "relevance": "score desc",
    "newest": "dateUploaded desc",
    "oldest": "dateUploaded asc",
    "published_newest": "datePublished desc",
    "title": "titlestr asc",
    "size_desc": "size desc",
    "size_asc": "size asc",
}

# Index fields that the helpers in this module write into q and fq (bbox_clause, coverage_clauses, search).
QUERY_FIELDS = [
    "northBoundCoord",
    "southBoundCoord",
    "eastBoundCoord",
    "westBoundCoord",
    "beginDate",
    "endDate",
    "formatType",
    "formatId",
    "obsoletedBy",
]
FIELD_FLAGS = ("searchable", "returnable", "sortable", "multivalued")
# Field types Solr refuses to sort on ("Sorting not supported on SpatialField"), though the index lists them as sortable.
UNSORTABLE_TYPES = {"date_range"}

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


def coverage_clauses(
    north: float | None,
    south: float | None,
    east: float | None,
    west: float | None,
    start_date: str | None,
    end_date: str | None,
    mode: str = "intersects",
) -> list[str]:
    """Clauses for an optional bounding box (all four sides or none) and an optional data collection period
    (datasets whose beginDate/endDate overlap it)."""
    clauses: list[str] = []
    box = (north, south, east, west)
    if any(v is not None for v in box):
        if any(v is None for v in box):
            raise ToolError("Give all four of north, south, east and west, or none of them.")
        clauses.append(bbox_clause(north, south, east, west, mode))  # type: ignore[arg-type]
    if start_date or end_date:
        clauses.append(f"beginDate:[* TO {solr_date(end_date, end_of_period=True)}]")
        clauses.append(f"endDate:[{solr_date(start_date)} TO *]")
    return clauses


# Metadata a record can lack, as the Solr clause that matches records that HAVE it. Checked against the live index:
# The place and GCMD keyword fields (empty for every record) are left out because "missing" would be misleading.
# license is tested on licenseName, which only about 1% of records have, so most records show as lacking it.
PRESENT_CLAUSES = {
    "attributes": "attributeName:*",
    "units": "attributeUnit:*",
    "keywords": "keywords:*",
    "abstract": "abstract:*",
    "spatial_coverage": "northBoundCoord:*",
    "temporal_coverage": "beginDate:*",
    "funding": "(awardNumber:* OR funderName:* OR fundingText:*)",
    "semantic_annotations": "sem_annotation:*",
    "license": "licenseName:*",
}
# Index fields named in PRESENT_CLAUSES, for solr.check_fields (see tools.fields_in_use).
PRESENT_FIELDS = sorted({f for c in PRESENT_CLAUSES.values() for f in re.findall(r"(\w+):\*", c)})
# EML records only: portals, ISO 19115, Dublin Core and FGDC records have no EML attributes, funding or annotations.
EML_FORMAT_FQ = "formatId:(" + " OR ".join(term(p) for p in ("eml://*", "https://eml.ecoinformatics.org/*")) + ")"


def missing_clause(name: str) -> str:
    """Clause matching records that lack ``name`` (a key of PRESENT_CLAUSES)."""
    try:
        return f"-{PRESENT_CLAUSES[name]}"
    except KeyError:
        raise ToolError(f"Unknown metadata field {name!r}. Choose from: {', '.join(PRESENT_CLAUSES)}.") from None


def missing_filters(names: list[str], match: str = "all") -> list[str]:
    """Filter queries for records lacking all (one fq each) or any (one OR'd fq) of ``names``.

    Solr matches nothing for a negative clause inside an OR, so each is anchored on ``*:*`` there.
    """
    clauses = [missing_clause(n) for n in names]
    if match == "any" and len(clauses) > 1:
        return [" OR ".join(f"(*:* AND {c})" for c in clauses)]
    return clauses


def index_fields(description: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Fields of the index by name, from the parsed GET /query/solr description.

    Each has ``name``, ``type``, ``description`` and the boolean flags in ``FIELD_FLAGS``.
    """
    out: dict[str, dict[str, Any]] = {}
    for f in as_list(description.get("queryField")):
        if not isinstance(f, dict) or not f.get("name"):
            continue
        out[f["name"]] = {
            "name": f["name"],
            "type": f.get("type"),
            "description": (f.get("description") or "").strip() or None,
            **{flag: f.get(flag) == "true" for flag in FIELD_FLAGS},
        }
    return out


async def fetch_index_fields(client: DataONEClient) -> dict[str, dict[str, Any]]:
    return index_fields(await client.get_xml(client.mn("query/solr")))


@dataclass
class FieldUse:
    """Index field names this server hard-codes, grouped by what the index must allow for each."""

    returned: set[str] = field(default_factory=set)  # requested in fl
    searched: set[str] = field(default_factory=set)  # written into q or fq
    sorted_by: set[str] = field(default_factory=set)  # written into sort


def sort_fields(sorts: Any) -> set[str]:
    """Field names in Solr sort strings such as 'dateUploaded desc'. 'score' is not an index field."""
    return {s.split()[0] for s in sorts} - {"score"}


def check_fields(index: dict[str, dict[str, Any]], use: FieldUse) -> list[str]:
    """Compare the field names we use with the index. Returns one message per problem, naming the field.

    A field we request in fl that the index drops is silently left out of results, and one we search or
    sort on makes the query fail, so both need a code change here.
    """
    problems = []
    for flag, names in (("returnable", use.returned), ("searchable", use.searched), ("sortable", use.sorted_by)):
        for name in sorted(names):
            if name not in index:
                problems.append(f"{name}: not in the index (we need it {flag})")
            elif not index[name][flag]:
                problems.append(f"{name}: in the index but not {flag}")
            elif flag == "sortable" and index[name]["type"] in UNSORTABLE_TYPES:
                problems.append(f"{name}: type {index[name]['type']} cannot be sorted on")
    return problems


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
        out["bbox"] = {k: _first(doc.get(f"{k}BoundCoord")) for k in ("north", "south", "east", "west")}
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
    facet_queries: dict[str, str] | None = None,
    raw: bool = False,
) -> dict[str, Any]:
    """Run a search and return ``{numFound, latest_only, start, rows, query, results}``.

    ``latest_only`` is echoed so callers can tell whether ``numFound`` counts only current versions.
    ``facet_queries`` maps a key to a Solr query; the same request then also returns ``facet_queries``, how many of
    the matching documents each query matches.
    """
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
    if facet_queries:
        params.append(("facet", "true"))
        params += [("facet.query", f"{{!key={key}}}{clause}") for key, clause in facet_queries.items()]
    data = await client.solr(params)
    resp = data.get("response", {})
    docs = resp.get("docs", [])
    out: dict[str, Any] = {
        "numFound": resp.get("numFound", 0),
        "latest_only": latest_only,
        "start": resp.get("start", 0),
        "rows": len(docs),
        "query": {"q": query, "fq": fq},
        "results": docs if raw else [summarize(d, client) for d in docs],
    }
    if facet_queries:
        out["facet_queries"] = data.get("facet_counts", {}).get("facet_queries", {})
    return out
