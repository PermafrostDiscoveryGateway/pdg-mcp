"""Project portal tools: discover Arctic Data Center portals and search the datasets each one collects.

A portal is a versioned DataONE object (format portals-1.0.0 or -1.1.0). The indexer turns its ``definition``
into a ready-to-run Solr query in the ``collectionQuery`` field. Dataset counts and reverse lookups run those
queries as batched ``facet.query`` requests, so covering every portal costs a few Solr calls, not one each.
Only public portals are visible, since requests are anonymous.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastmcp import FastMCP
from fastmcp.apps import AppConfig
from fastmcp.exceptions import ToolError
from pydantic import Field

from arcticdata_mcp import solr
from arcticdata_mcp.client import DataONEClient, as_list
from arcticdata_mcp.config import PortalSettings
from arcticdata_mcp.tools.apps import SEARCH_MAP_URI
from arcticdata_mcp.tools.common import READ_ONLY, DateStr, Pid, Rows, Sort, Start, ToolContext

PORTAL_FORMATS = ["https://purl.dataone.org/portals-1.0.0", "https://purl.dataone.org/portals-1.1.0"]
PORTAL_FORMAT_CLAUSE = "formatId:(" + " OR ".join(solr.phrase(f) for f in PORTAL_FORMATS) + ")"
PORTAL_FIELDS = ["id", "seriesId", "label", "title", "abstract", "collectionQuery", "logo", "dateUploaded"]
# The fields the Arctic Data Center portal list searches (NCEAS/metacatui#2953), so results match the website.
SEARCH_FIELDS = [
    "title",
    "abstract",
    "label",
    "investigator",
    "funderName",
    "funderIdentifier",
    "awardNumber",
    "collectionQuery",
]
# Fields written directly into query strings in this module, and the fields and sort of the dataset lookup
# in find_portals_for_dataset. solr.check_fields compares all of these with the index (see tools.fields_in_use).
QUERY_FIELDS = ["formatId", "obsoletedBy", "id", "seriesId"]
DATASET_LOOKUP_FIELDS = ["id", "title", "obsoletedBy"]
DATASET_LOOKUP_SORT = "dateUploaded desc"
SORTS = [DATASET_LOOKUP_SORT]
# Collection queries per facet request, by total length, to keep the GET URL well under server limits.
FACET_BATCH_CHARS = 4000
DESCRIPTION_CHARS = 300
DEFINITION_VALUES = 50

Lat = Annotated[float | None, Field(ge=-90, le=90)]
Lon = Annotated[float | None, Field(ge=-180, le=180)]
PortalKey = Annotated[
    str, Field(description="Portal label (e.g. 'PerCS-Net', as in the portal URL), series ID or PID.")
]


def _batches(portals: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    out: list[list[dict[str, Any]]] = []
    size = FACET_BATCH_CHARS
    for p in portals:
        n = len(p["collectionQuery"])
        if size + n > FACET_BATCH_CHARS:
            out.append([])
            size = 0
        out[-1].append(p)
        size += n
    return out


async def _facet_counts(client: DataONEClient, q: str, portals: list[dict[str, Any]]) -> dict[str, int | None]:
    params: list[tuple[str, Any]] = [("q", q), ("rows", 0), ("facet", "true")]
    params += [("facet.query", f"{{!key=p{i}}}({p['collectionQuery']})") for i, p in enumerate(portals)]
    try:
        data = await client.solr(params)
    except ToolError:
        if len(portals) == 1:
            return {portals[0]["id"]: None}
        # One malformed collection query fails the whole batch; retry singly to isolate it.
        parts = await asyncio.gather(*(_facet_counts(client, q, [p]) for p in portals))
        return {k: v for part in parts for k, v in part.items()}
    counts = data.get("facet_counts", {}).get("facet_queries", {})
    return {p["id"]: counts.get(f"p{i}") for i, p in enumerate(portals)}


async def count_matches(client: DataONEClient, q: str, portals: list[dict[str, Any]]) -> dict[str, int | None]:
    """Count the documents matching ``q`` inside each portal's collection. None = the portal's query failed."""
    runnable = [p for p in portals if p.get("collectionQuery")]
    parts = await asyncio.gather(*(_facet_counts(client, q, b) for b in _batches(runnable)))
    counts: dict[str, int | None] = {p["id"]: None for p in portals}
    for part in parts:
        counts.update(part)
    return counts


class PortalIndex:
    """The current public portals with their dataset counts, cached for ``cache_ttl`` seconds."""

    def __init__(self, client: DataONEClient, settings: PortalSettings):
        self.client = client
        self.settings = settings
        self._lock = asyncio.Lock()
        self._portals: list[dict[str, Any]] | None = None
        self._fetched = 0.0

    async def portals(self) -> list[dict[str, Any]]:
        async with self._lock:
            if self._portals is None or time.monotonic() - self._fetched >= self.settings.cache_ttl:
                self._portals = await self._load()
                self._fetched = time.monotonic()
            return self._portals

    async def _load(self) -> list[dict[str, Any]]:
        data = await self.client.solr(
            [
                ("q", PORTAL_FORMAT_CLAUSE),
                ("fq", "-obsoletedBy:*"),
                ("fl", ",".join(PORTAL_FIELDS)),
                ("rows", 1000),
            ]
        )
        excluded = {label.lower() for label in self.settings.exclude_labels}
        portals = [
            d for d in data.get("response", {}).get("docs", []) if (d.get("label") or "").lower() not in excluded
        ]
        counts = await count_matches(self.client, "*:*", portals)
        for p in portals:
            p["dataset_count"] = counts[p["id"]]
        return sorted(portals, key=lambda p: (p.get("title") or p.get("label") or "").lower())

    async def resolve(self, key: str) -> dict[str, Any]:
        key = key.strip()
        portals = await self.portals()
        for p in portals:
            if key.lower() == (p.get("label") or "").lower() or key in (p.get("seriesId"), p["id"]):
                return p
        raise ToolError(f"No public portal '{key}'. Use search_portals to find portal labels.")


def _text(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("#text")
    if isinstance(value, list):
        value = "\n\n".join(t for t in (_text(v) for v in value) if t)
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _truncate(text: str | None, limit: int) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + "…"


def _definition(defn: Any) -> list[dict[str, Any]]:
    """Flatten the portal definition's filters into a compact, readable list."""
    out = []
    for kind, items in (defn or {}).items():
        if not isinstance(items, (dict, list)):
            continue
        for f in as_list(items):
            if not isinstance(f, dict):
                continue
            values = [_text(v) for v in as_list(f.get("value"))]
            entry: dict[str, Any] = {"type": kind, "fields": as_list(f.get("field"))}
            if f.get("label"):
                entry["label"] = _text(f["label"])
            if values:
                entry["values"] = values[:DEFINITION_VALUES]
                if len(values) > DEFINITION_VALUES:
                    entry["values_truncated"] = len(values)
            for k in ("operator", "fieldsOperator", "exclude", "matchSubstring", "min", "max"):
                if k in f:
                    entry[k] = _text(f[k])
            out.append(entry)
    return out


def _section(s: dict[str, Any], max_chars: int) -> dict[str, Any]:
    options = {
        _text(o.get("optionName")): o.get("optionValue") for o in as_list(s.get("option")) if isinstance(o, dict)
    }
    markdown = _text((s.get("content") or {}).get("markdown")) if isinstance(s.get("content"), dict) else None
    out = {
        "label": _text(s.get("label")),
        "title": _text(s.get("title")),
        "type": _text(options.get("sectionType")) or "freeform",
        "introduction": _text(s.get("introduction")),
        "markdown": _truncate(markdown, max_chars),
    }
    if markdown and len(markdown) > max_chars:
        out["markdown_chars"] = len(markdown)
    return {k: v for k, v in out.items() if v is not None}


def _person(p: dict[str, Any]) -> dict[str, Any]:
    name = p.get("individualName") or {}
    full = " ".join(t for t in (_text(n) for n in [*as_list(name.get("givenName")), name.get("surName")]) if t)
    out = {
        "name": full or None,
        "organization": _text(p.get("organizationName")),
        "position": _text(p.get("positionName")),
    }
    return {k: v for k, v in out.items() if v}


def _award(a: dict[str, Any]) -> dict[str, Any]:
    out = {k: _text(a.get(k)) for k in ("title", "awardNumber", "funderName", "funderIdentifier")}
    return {k: v for k, v in out.items() if v}


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    client = ctx.client
    settings = ctx.settings.portals
    index = PortalIndex(client, settings)

    def portal_url(p: dict[str, Any]) -> str:
        return f"{settings.portal_url.rstrip('/')}/{quote(p.get('label') or p.get('seriesId') or p['id'], safe='')}"

    def summary(p: dict[str, Any]) -> dict[str, Any]:
        return {
            "label": p.get("label"),
            "title": p.get("title"),
            "description": _truncate(p.get("abstract"), DESCRIPTION_CHARS),
            "portal_url": portal_url(p),
            "dataset_count": p.get("dataset_count"),
            "id": p["id"],
            "seriesId": p.get("seriesId"),
        }

    @mcp.tool(annotations=READ_ONLY, tags={"portals"})
    async def search_portals(
        query: Annotated[
            str | None,
            Field(
                description="Optional keywords matched against portal title, description, label, investigators, funders, award numbers and collection query. All words must match; quote a phrase to keep it together. Omit to list every portal."
            ),
        ] = None,
        sort: Annotated[
            Literal["relevance", "title", "datasets"],
            Field(
                description="'relevance' (with a query; title otherwise), 'title', or 'datasets' (most datasets first)."
            ),
        ] = "relevance",
        rows: Rows = 20,
        start: Start = 0,
    ) -> dict[str, Any]:
        """Find Arctic Data Center project portals (curated collections such as PerCS-Net, SASAP or GreenTRACS).

        Returns each portal's label, title, description, website URL and number of datasets. A dataset_count
        of 0 is normal for a new or empty portal; null means the portal's collection query failed to run.
        Next: get_portal for its description and sections, search_portal_datasets for its data.
        Example: search_portals(query="coastal permafrost")
        """
        portals = await index.portals()
        if query:
            words = [w.strip('"') for w in re.findall(r'"[^"]+"|\S+', query)]
            words = [w for w in words if w.strip()]
            if not words:
                raise ToolError("query must contain at least one word")
            data = await client.solr(
                [
                    ("q", " AND ".join(solr.any_field(SEARCH_FIELDS, w) for w in words)),
                    ("fq", PORTAL_FORMAT_CLAUSE),
                    ("fq", "-obsoletedBy:*"),
                    ("fl", "id"),
                    ("rows", 1000),
                    ("sort", "score desc"),
                ]
            )
            by_id = {p["id"]: p for p in portals}
            hits = [by_id[d["id"]] for d in data.get("response", {}).get("docs", []) if d["id"] in by_id]
        else:
            hits = list(portals)
        if sort == "datasets":
            hits.sort(key=lambda p: p.get("dataset_count") or 0, reverse=True)
        elif sort == "title" and query:
            hits.sort(key=lambda p: (p.get("title") or p.get("label") or "").lower())
        page = hits[start : start + rows]
        return {
            "numFound": len(hits),
            "start": start,
            "rows": len(page),
            "query": query,
            "portals": [summary(p) for p in page],
        }

    @mcp.tool(annotations=READ_ONLY, tags={"portals"})
    async def get_portal(
        portal: PortalKey,
        max_section_chars: Annotated[
            int, Field(ge=0, le=50000, description="Truncate each section's markdown to this many characters.")
        ] = 4000,
    ) -> dict[str, Any]:
        """Get a project portal's full description: its content sections (About, Publications, ... as
        markdown), people, funding awards, logo, the rules that pick its datasets, and the resulting
        Solr collection query.

        Example: get_portal(portal="PerCS-Net")
        """
        p = await index.resolve(portal)
        doc = await client.get_xml(client.object_url(p["id"]))
        logo = doc.get("logo") or {}
        logo_id = _text(logo.get("identifier")) if isinstance(logo, dict) else None
        return {
            "id": p["id"],
            "seriesId": p.get("seriesId"),
            "label": _text(doc.get("label")) or p.get("label"),
            "name": _text(doc.get("name")) or p.get("title"),
            "description": _text(doc.get("description")) or p.get("abstract"),
            "portal_url": portal_url(p),
            "logo_url": client.object_url(logo_id) if logo_id else None,
            "dataset_count": p.get("dataset_count"),
            "updated": p.get("dateUploaded"),
            "collection_query": p.get("collectionQuery"),
            "definition": _definition(doc.get("definition")),
            "awards": [_award(a) for a in as_list(doc.get("award")) if isinstance(a, dict)],
            "people": [_person(a) for a in as_list(doc.get("associatedParty")) if isinstance(a, dict)],
            "sections": [_section(s, max_section_chars) for s in as_list(doc.get("section")) if isinstance(s, dict)],
        }

    @mcp.tool(annotations=READ_ONLY, tags={"portals", "search", "spatial"}, app=AppConfig(resource_uri=SEARCH_MAP_URI))
    async def search_portal_datasets(
        portal: PortalKey,
        text: Annotated[
            str | None,
            Field(description="Optional free-text query within the portal. Solr syntax (AND/OR/quotes) is allowed."),
        ] = None,
        north: Lat = None,
        south: Lat = None,
        east: Lon = None,
        west: Lon = None,
        start_date: DateStr = None,
        end_date: DateStr = None,
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "relevance",
    ) -> dict[str, Any]:
        """List or search the datasets in a project portal, exactly as the portal's own rules select them
        (access groups, hand-picked datasets, awards, authors, ...), optionally narrowed by text, a
        bounding box (all four of north/south/east/west; footprints that intersect it) and a data
        collection period (start_date/end_date).

        Example: search_portal_datasets(portal="PerCS-Net", text="erosion", start_date="2018")
        """
        p = await index.resolve(portal)
        if not p.get("collectionQuery"):
            raise ToolError(f"Portal '{portal}' has no indexed collection query.")
        q: list[str] = []
        if text:
            q.append(text)
        box = (north, south, east, west)
        if any(v is not None for v in box):
            if any(v is None for v in box):
                raise ToolError("Give all four of north, south, east and west, or none of them.")
            q.append(solr.bbox_clause(north, south, east, west))  # type: ignore[arg-type]
        if start_date or end_date:
            q += solr.coverage_clauses(start_date, end_date)
        res = await solr.search(client, q, rows=rows, start=start, sort=sort, extra_fq=[p["collectionQuery"]])
        return {"portal": {"label": p.get("label"), "title": p.get("title"), "portal_url": portal_url(p)}, **res}

    @mcp.tool(annotations=READ_ONLY, tags={"portals"})
    async def find_portals_for_dataset(pid: Pid) -> dict[str, Any]:
        """Find the project portals that include a dataset (by its PID or series ID).

        Portals only collect the latest version of a dataset; an older PID is reported with the PID of
        its newer version.
        Example: find_portals_for_dataset(pid="doi:10.18739/A29K4B")
        """
        ident = solr.phrase(pid.strip())
        q = f"id:{ident} OR seriesId:{ident}"
        data = await client.solr(
            [
                ("q", q),
                ("fl", ",".join(DATASET_LOOKUP_FIELDS)),
                ("rows", 1),
                ("sort", DATASET_LOOKUP_SORT),
            ]
        )
        docs = data.get("response", {}).get("docs", [])
        if not docs:
            raise ToolError(f"No public object with PID or series ID '{pid}'.")
        doc = docs[0]
        out: dict[str, Any] = {"pid": doc["id"], "title": doc.get("title")}
        if doc.get("obsoletedBy"):
            out["note"] = (
                f"'{doc['id']}' is an older version (obsoleted by '{doc['obsoletedBy']}'). "
                "Portals only include latest versions; try the newer PID."
            )
        portals = await index.portals()
        counts = await count_matches(client, q, portals)
        matched = [summary(p) for p in portals if counts[p["id"]]]
        failed = [p.get("label") or p["id"] for p in portals if p.get("collectionQuery") and counts[p["id"]] is None]
        out["numFound"] = len(matched)
        out["portals"] = matched
        if failed:
            out["unchecked_portals"] = failed
        return out
