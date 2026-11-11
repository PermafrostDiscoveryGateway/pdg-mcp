"""Discovery and search tools backed by the DataONE Solr index (GET /query/solr)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.apps import AppConfig
from fastmcp.exceptions import ToolError
from pydantic import Field

from arcticdata_mcp import solr
from arcticdata_mcp.client import as_list
from arcticdata_mcp.tools.apps import SEARCH_MAP_URI
from arcticdata_mcp.tools.common import (
    READ_ONLY,
    DateStr,
    LatestOnly,
    Pid,
    Rows,
    Sort,
    Start,
    ToolContext,
)

Lat = Annotated[float, Field(ge=-90, le=90)]
Lon = Annotated[float, Field(ge=-180, le=180)]

PROV_FIELDS = {
    "was_derived_from": "prov_wasDerivedFrom",
    "has_derivations": "prov_hasDerivations",
    "used": "prov_used",
    "was_generated_by": "prov_wasGeneratedBy",
    "generated": "prov_generated",
    "has_sources": "prov_hasSources",
    "used_by_program": "prov_usedByProgram",
    "generated_by_program": "prov_generatedByProgram",
    "was_informed_by": "prov_wasInformedBy",
}


# Solr fields each search_datasets filter matches (a value matching any one of them counts).
FIELD_FILTERS = {
    "title": ["title"],
    "author": ["originText", "author", "authorLastName", "investigatorText"],
    "keyword": ["keywordsText", "gcmdKeyword", "topicText"],
    "place": ["placeKey", "siteText", "namedLocation"],
    "organization": ["contactOrganizationText", "relatedOrganizations"],
    "project": ["projectText", "project"],
    "funding": ["awardNumber", "fundingText", "funderName", "awardTitle"],
    "variable": ["attributeName", "attributeLabel", "attributeDescription", "attributeUnit", "attribute"],
    "taxon": ["scientificName", "kingdom", "phylum", "class", "order", "family", "genus", "species"],
    "concept_uri": ["sem_annotation"],
    "owner": ["rightsHolder", "submitter"],
}


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    client = ctx.client

    async def run(q: str | list[str], **kw: Any) -> dict[str, Any]:
        return await solr.search(client, q, **kw)

    @mcp.tool(annotations=READ_ONLY, tags={"search", "spatial", "temporal"}, app=AppConfig(resource_uri=SEARCH_MAP_URI))
    async def search_datasets(
        query: Annotated[str | None, Field(description="Free text over all metadata, e.g. 'permafrost thaw methane'. Solr syntax (AND/OR/quotes) is allowed.")] = None,
        title: Annotated[str | None, Field(description="Title phrase or wildcard pattern, e.g. '*snow depth*'.")] = None,
        author: Annotated[str | None, Field(description="Creator, author surname or investigator, e.g. 'Romanovsky'.")] = None,
        keyword: Annotated[str | None, Field(description="Author, GCMD or topic keyword, e.g. 'active layer'.")] = None,
        place: Annotated[str | None, Field(description="Named place or site, e.g. 'Toolik Lake'.")] = None,
        organization: Annotated[str | None, Field(description="Contact or related organization.")] = None,
        project: Annotated[str | None, Field(description="Research project, e.g. 'Arctic Observing Network'.")] = None,
        funding: Annotated[str | None, Field(description="Award number (e.g. '1503559'), funder name or award title.")] = None,
        variable: Annotated[str | None, Field(description="Measured variable (data-table attribute) name, label, description or unit, e.g. 'soil temperature'.")] = None,
        taxon: Annotated[str | None, Field(description="Scientific name or taxon at any rank, e.g. 'Rangifer tarandus'.")] = None,
        concept_uri: Annotated[str | None, Field(description="Semantic annotation concept URI, e.g. 'http://purl.dataone.org/odo/ECSO_00001197'.")] = None,
        license: Annotated[str | None, Field(description="License name fragment (case-sensitive), e.g. 'Creative Commons Zero'.")] = None,
        owner: Annotated[str | None, Field(description="Rights holder or submitter ORCID, e.g. 'http://orcid.org/0000-0003-1846-5140'.")] = None,
        north: Lat | None = None,
        south: Lat | None = None,
        east: Lon | None = None,
        west: Lon | None = None,
        spatial_mode: Annotated[
            Literal["intersects", "within"],
            Field(description="Bounding box test: footprint overlaps the box, or lies fully inside it."),
        ] = "intersects",
        start_date: DateStr = None,
        end_date: DateStr = None,
        uploaded_from: DateStr = None,
        uploaded_to: DateStr = None,
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "relevance",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Search Arctic Data Center datasets. Every filter is optional and filters combine with AND;
        with none, all datasets are listed. Text filters accept wildcards ('*', '?').

        start_date/end_date match the data collection period (overlap); uploaded_from/uploaded_to match the
        upload date ('NOW-30DAYS' with sort='newest' shows what's new). The bounding box needs all of
        north/south/east/west in WGS84 degrees and cannot cross the antimeridian. rows=0 returns only numFound.
        get_facet_counts shows valid filter values; advanced_solr_query targets a single Solr field.
        Example: search_datasets(keyword="permafrost", author="Schuur", start_date="2010")
        """
        q: list[str] = []
        if query:
            q.append(query)
        given = {"title": title, "author": author, "keyword": keyword, "place": place, "organization": organization,
                 "project": project, "funding": funding, "variable": variable, "taxon": taxon,
                 "concept_uri": concept_uri, "owner": owner}
        q += [solr.any_field(FIELD_FILTERS[k], v) for k, v in given.items() if v]
        if license:
            value = license if ("*" in license or "?" in license) else f"*{license}*"
            q.append(solr.any_field(["licenseName", "licenseUrl"], value))
        box = (north, south, east, west)
        if any(v is not None for v in box):
            if any(v is None for v in box):
                raise ToolError("Give all four of north, south, east and west, or none of them.")
            q.append(solr.bbox_clause(north, south, east, west, spatial_mode))  # type: ignore[arg-type]
        if start_date or end_date:
            q.append(f"beginDate:[* TO {solr.solr_date(end_date, end_of_period=True)}]")
            q.append(f"endDate:[{solr.solr_date(start_date)} TO *]")
        if uploaded_from or uploaded_to:
            q.append(solr.date_range("dateUploaded", uploaded_from, uploaded_to))
        return await run(q, rows=rows, start=start, sort=sort, latest_only=latest_only)

    @mcp.tool(annotations=READ_ONLY, tags={"search", "provenance"})
    async def search_by_provenance(
        pid: Pid,
        relation: Annotated[
            Literal[tuple(PROV_FIELDS)],  # type: ignore[valid-type]
            Field(description="Provenance relation, read as '<result> <relation> <pid>', e.g. was_derived_from = objects derived from pid."),
        ] = "was_derived_from",
        rows: Rows = 10,
        start: Start = 0,
    ) -> dict[str, Any]:
        """Find objects linked to a PID by a PROV relationship (derivations, sources, programs, executions).

        Example: search_by_provenance(pid="urn:uuid:...", relation="has_derivations")
        """
        return await run(
            f"{PROV_FIELDS[relation]}:{solr.phrase(pid)}",
            rows=rows, start=start, format_type=None, latest_only=False,
            fl=[*solr.METADATA_FIELDS, "fileName", "formatType", "size"],
        )

    @mcp.tool(annotations=READ_ONLY, tags={"search", "files"})
    async def search_data_files(
        file_name: Annotated[str | None, Field(description="File name or wildcard pattern, e.g. '*.nc', 'ODEN-*'.")] = None,
        format_id: Annotated[str | None, Field(description="DataONE format ID, e.g. 'text/csv', 'netCDF-4', 'image/tiff'.")] = None,
        min_size: Annotated[int | None, Field(ge=0, description="Minimum size in bytes.")] = None,
        max_size: Annotated[int | None, Field(ge=0, description="Maximum size in bytes.")] = None,
        documented_by: Annotated[str | None, Field(description="Only files documented by this metadata PID.")] = None,
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "relevance",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Search raw data files (not datasets) by file name, format and size. Returns download URLs.

        Example: search_data_files(file_name="*.nc", max_size=50000000)
        """
        q: list[str] = []
        if file_name:
            q.append(f"fileName:{solr.term(file_name)}")
        if format_id:
            q.append(f"formatId:{solr.phrase(format_id)}")
        if min_size is not None or max_size is not None:
            q.append(f"size:[{min_size if min_size is not None else '*'} TO {max_size if max_size is not None else '*'}]")
        if documented_by:
            q.append(f"isDocumentedBy:{solr.phrase(documented_by)}")
        if not q:
            raise ToolError("Provide at least one filter")
        return await run(q, rows=rows, start=start, sort=sort, latest_only=latest_only, format_type="DATA")

    @mcp.tool(annotations=READ_ONLY, tags={"search"})
    async def find_dataset_by_identifier(
        identifier: Annotated[str, Field(description="A DOI ('doi:10.18739/A2...' or 'https://doi.org/10.18739/...'), PID, series ID, or a data file PID.")],
    ) -> dict[str, Any]:
        """Resolve an identifier to its dataset(s): matches the PID, series ID, alternate identifiers,
        or—if a data-file PID is given—the dataset(s) that document it.

        Example: find_dataset_by_identifier(identifier="https://doi.org/10.18739/A29K4B")
        """
        ident = identifier.strip()
        for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/"):
            if ident.lower().startswith(prefix):
                ident = "doi:" + ident[len(prefix):]
        p = solr.phrase(ident)
        return await run(
            f"id:{p} OR seriesId:{p} OR identifier:{p} OR documents:{p}",
            rows=20, latest_only=False,
        )

    @mcp.tool(annotations=READ_ONLY, tags={"search", "facets"})
    async def get_facet_counts(
        field: Annotated[str, Field(description="Solr field to facet on, e.g. 'keywords', 'formatId', 'project', 'funding', 'licenseName', 'authorLastName', 'placeKey', 'sem_annotation'.")],
        query: Annotated[str | None, Field(description="Optional Solr/free-text query restricting the documents counted.")] = None,
        limit: Annotated[int, Field(ge=1, le=500, description="Maximum number of values.")] = 50,
        prefix: Annotated[str | None, Field(description="Only values starting with this prefix.")] = None,
        format_type: Annotated[
            Literal["METADATA", "DATA", "RESOURCE", "any"],
            Field(description="Which object type to count over."),
        ] = "METADATA",
    ) -> dict[str, Any]:
        """List the most common values of a field with document counts (e.g. top keywords, formats, projects, licenses).

        Great for discovering valid filter values before searching.
        Example: get_facet_counts(field="formatId", format_type="DATA")
        """
        fq = ["-obsoletedBy:*"]
        if format_type != "any":
            fq.append(f"formatType:{format_type}")
        params: list[tuple[str, Any]] = [
            ("q", query or "*:*"), ("rows", 0), ("facet", "true"), ("facet.field", field),
            ("facet.limit", limit), ("facet.mincount", 1), ("facet.sort", "count"),
        ]
        if prefix:
            params.append(("facet.prefix", prefix))
        params += [("fq", f) for f in fq]
        data = await client.solr(params)
        flat = data.get("facet_counts", {}).get("facet_fields", {}).get(field, [])
        values = [{"value": flat[i], "count": flat[i + 1]} for i in range(0, len(flat) - 1, 2)]
        return {"field": field, "total_documents": data.get("response", {}).get("numFound"), "values": values}

    @mcp.tool(annotations=READ_ONLY, tags={"search", "reference"})
    async def list_search_fields(
        name_contains: Annotated[str | None, Field(description="Optional case-insensitive filter on field name.")] = None,
    ) -> dict[str, Any]:
        """List every searchable Solr index field (name, type, description, multivalued) from GET /query/solr.

        Use this to build advanced_solr_query calls.
        """
        desc = await client.get_xml(client.mn("query/solr"))
        fields = []
        for f in as_list(desc.get("queryField")):
            name = f.get("name")
            if name_contains and name_contains.lower() not in (name or "").lower():
                continue
            fields.append({
                "name": name,
                "type": f.get("type"),
                "description": (f.get("description") or "").strip() or None,
                "searchable": f.get("searchable") == "true",
                "returnable": f.get("returnable") == "true",
                "multivalued": f.get("multivalued") == "true",
            })
        return {"engine": desc.get("name"), "version": desc.get("queryEngineVersion"), "count": len(fields), "fields": fields}

    @mcp.tool(annotations=READ_ONLY, tags={"search", "advanced"})
    async def advanced_solr_query(
        q: Annotated[str, Field(description="Solr q parameter, e.g. 'keywords:permafrost AND beginDate:[2010-01-01T00:00:00Z TO *]'.")],
        fq: Annotated[list[str] | None, Field(description="Filter queries, e.g. ['formatType:METADATA', '-obsoletedBy:*'].")] = None,
        fl: Annotated[str | None, Field(description="Comma-separated fields to return (see list_search_fields). Default: summary fields.")] = None,
        sort: Annotated[str | None, Field(description="Solr sort, e.g. 'dateUploaded desc'.")] = None,
        rows: Rows = 10,
        start: Start = 0,
        facet_fields: Annotated[list[str] | None, Field(description="Optional fields to facet on.")] = None,
    ) -> dict[str, Any]:
        """Run a raw Solr query against the Arctic Data Center index, with full control over q/fq/fl/sort/facets.

        Returns raw Solr documents. Only public content is visible.
        """
        params: list[tuple[str, Any]] = [
            ("q", q), ("rows", min(rows, solr.MAX_ROWS)), ("start", start),
            ("fl", fl or ",".join(sorted({*solr.METADATA_FIELDS, *solr.DATA_FIELDS}))),
        ]
        if sort:
            params.append(("sort", sort))
        params += [("fq", f) for f in fq or []]
        if facet_fields:
            params += [("facet", "true"), ("facet.mincount", 1), ("facet.limit", 50)]
            params += [("facet.field", f) for f in facet_fields]
        data = await client.solr(params)
        resp = data.get("response", {})
        out: dict[str, Any] = {"numFound": resp.get("numFound"), "start": resp.get("start"), "docs": resp.get("docs", [])}
        if facet_fields:
            ff = data.get("facet_counts", {}).get("facet_fields", {})
            out["facets"] = {k: [{"value": v[i], "count": v[i + 1]} for i in range(0, len(v) - 1, 2)] for k, v in ff.items()}
        return out
