"""Discovery and search tools backed by the DataONE Solr index (GET /query/solr)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.apps import AppConfig
from fastmcp.exceptions import ToolError
from pydantic import Field

from arcticdata_mcp import ontology, solr
from arcticdata_mcp.client import as_list
from arcticdata_mcp.tools.apps import SEARCH_MAP_URI
from arcticdata_mcp.tools.common import (
    READ_ONLY,
    DateStr,
    Lat,
    LatestOnly,
    Lon,
    Pid,
    Rows,
    Sort,
    Start,
    ToolContext,
)

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


TAXON_RANKS = ["scientificName", "kingdom", "phylum", "class", "order", "family", "genus", "species"]

# Solr fields each search_datasets filter matches (a value matching any one of them counts), keyed by
# filter and then by the filter's narrowing option; "any" is the default.
FIELD_FILTERS: dict[str, dict[str, list[str]]] = {
    "title": {"any": ["title"]},
    "author": {
        "any": ["originText", "author", "authorLastName", "authorSurName", "investigatorText"],
        "creator": ["originText", "author"],
        "last_name": ["authorLastName", "authorSurName"],
        "investigator": ["investigatorText"],
    },
    "keyword": {
        "any": ["keywordsText", "gcmdKeyword", "topicText"],
        "author_keywords": ["keywordsText"],
        "gcmd": ["gcmdKeyword"],
        "topic": ["topicText"],
    },
    "place": {"any": ["placeKey", "siteText", "namedLocation"]},
    "organization": {"any": ["contactOrganizationText", "relatedOrganizations"]},
    "project": {"any": ["projectText", "project"]},
    "funding": {
        "any": ["awardNumber", "fundingText", "funderName", "awardTitle"],
        "award_number": ["awardNumber", "fundingText"],
        "funder_name": ["funderName"],
        "award_title": ["awardTitle"],
    },
    "variable": {
        "any": solr.ATTRIBUTE_FIELDS,
        "name": ["attributeName"],
        "label": ["attributeLabel"],
        "description": ["attributeDescription"],
        "unit": ["attributeUnit"],
    },
    "taxon": {"any": TAXON_RANKS, **{r: [r] for r in TAXON_RANKS}},
}
OWNER_FIELDS = {
    "either": ["rightsHolder", "submitter"],
    "rights_holder": ["rightsHolder"],
    "submitter": ["submitter"],
}
LICENSE_FIELDS = ["licenseName", "licenseUrl"]
RECORD_DATE_FIELDS = ("dateUploaded", "datePublished", "dateModified")
# Fields written directly into query strings in this module.
INLINE_QUERY_FIELDS = [
    "fileName",
    "formatId",
    "formatType",
    "size",
    "isDocumentedBy",
    "id",
    "seriesId",
    "identifier",
    "documents",
    "obsoletedBy",
]
# Every index field this module searches. solr.check_fields compares them with the index (see tools.fields_in_use),
# so keep field names in the lists above rather than inline in the tools.
QUERY_FIELDS = {
    *INLINE_QUERY_FIELDS,
    *PROV_FIELDS.values(),
    *LICENSE_FIELDS,
    *RECORD_DATE_FIELDS,
    *(f for options in FIELD_FILTERS.values() for fields in options.values() for f in fields),
    *(f for fields in OWNER_FIELDS.values() for f in fields),
}
# Returned by search_by_provenance and search_data_files on top of the default summary fields.
EXTRA_RETURN_FIELDS = ["fileName", "formatType", "size", "title"]
OwnerRole = Annotated[
    Literal["either", "rights_holder", "submitter"],
    Field(description="Match owner against the object's rightsHolder, its submitter, or either."),
]


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    client = ctx.client

    async def run(q: str | list[str], **kw: Any) -> dict[str, Any]:
        return await solr.search(client, q, **kw)

    @mcp.tool(annotations=READ_ONLY, tags={"search", "spatial", "temporal"}, app=AppConfig(resource_uri=SEARCH_MAP_URI))
    async def search_datasets(
        query: Annotated[
            str | None,
            Field(
                description="Free text over all metadata, e.g. 'permafrost thaw methane'. Solr syntax (AND/OR/quotes) is allowed."
            ),
        ] = None,
        title: Annotated[
            str | None, Field(description="Title phrase or wildcard pattern, e.g. '*snow depth*'.")
        ] = None,
        author: Annotated[
            str | None, Field(description="Creator, author surname or investigator, e.g. 'Romanovsky'.")
        ] = None,
        author_role: Annotated[
            Literal["any", "creator", "last_name", "investigator"],
            Field(description="Narrow author to dataset creators, author surnames only, or PIs/associated parties."),
        ] = "any",
        keyword: Annotated[str | None, Field(description="Author, GCMD or topic keyword, e.g. 'active layer'.")] = None,
        keyword_vocabulary: Annotated[
            Literal["any", "author_keywords", "gcmd", "topic"],
            Field(description="Narrow keyword to author-supplied keywords, GCMD science keywords or topic categories."),
        ] = "any",
        place: Annotated[str | None, Field(description="Named place or site, e.g. 'Toolik Lake'.")] = None,
        organization: Annotated[str | None, Field(description="Contact or related organization.")] = None,
        project: Annotated[str | None, Field(description="Research project, e.g. 'Arctic Observing Network'.")] = None,
        funding: Annotated[
            str | None, Field(description="Award number (e.g. '1503559'), funder name or award title.")
        ] = None,
        funding_field: Annotated[
            Literal["any", "award_number", "funder_name", "award_title"],
            Field(description="Narrow funding to one funding field."),
        ] = "any",
        variable: Annotated[
            str | None,
            Field(
                description="Measured variable (data-table attribute) name, label, description or unit, e.g. 'soil temperature'."
            ),
        ] = None,
        variable_field: Annotated[
            Literal["any", "name", "label", "description", "unit"],
            Field(description="Narrow variable to one attribute field."),
        ] = "any",
        taxon: Annotated[
            str | None, Field(description="Scientific name or taxon at any rank, e.g. 'Rangifer tarandus'.")
        ] = None,
        taxon_rank: Annotated[
            Literal["any", "scientificName", "kingdom", "phylum", "class", "order", "family", "genus", "species"],
            Field(description="Narrow taxon to one taxonomic rank."),
        ] = "any",
        concept_uri: Annotated[
            str | None,
            Field(
                description="Semantic annotation concept URI, e.g. 'http://purl.dataone.org/odo/ECSO_00001207' (ECSO 'Soil Depth'), a bare ECSO id such as 'ECSO_00001207', or a wildcard. http and https PURLs both match."
            ),
        ] = None,
        license: Annotated[
            str | None, Field(description="License name fragment (case-sensitive), e.g. 'Creative Commons Zero'.")
        ] = None,
        owner: Annotated[
            str | None,
            Field(description="Rights holder or submitter ORCID, e.g. 'http://orcid.org/0000-0003-1846-5140'."),
        ] = None,
        owner_role: OwnerRole = "either",
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
        record_date_from: DateStr = None,
        record_date_to: DateStr = None,
        record_date_field: Annotated[
            Literal[RECORD_DATE_FIELDS],  # type: ignore[valid-type]
            Field(description="Which record date record_date_from/record_date_to filter on."),
        ] = "dateUploaded",
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort | None = None,
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Search Arctic Data Center datasets. Every filter is optional and filters combine with AND;
        with none, all datasets are listed. Text filters accept wildcards ('*', '?').

        Each text filter matches several Solr fields; its *_role/*_vocabulary/*_field/*_rank option narrows it
        to one. start_date/end_date match the data collection period (overlap); record_date_from/record_date_to
        match the upload, publication or modification date ('NOW-30DAYS' shows what's new). The bounding box
        needs all of north/south/east/west in WGS84 degrees and cannot cross the antimeridian. sort defaults to
        'relevance' when a text filter is given and 'newest' otherwise. rows=0 returns only numFound.
        get_facet_counts shows valid filter values; advanced_solr_query targets a single Solr field.
        For "datasets that measure X" prefer search_by_variable, which maps X to ECSO concepts; the variable
        filter here is free text over attribute names, labels, descriptions and units.
        Example: search_datasets(keyword="permafrost", author="Schuur", start_date="2010")
        """
        q: list[str] = []
        if query:
            q.append(query)
        given = {
            "title": (title, "any"),
            "author": (author, author_role),
            "keyword": (keyword, keyword_vocabulary),
            "place": (place, "any"),
            "organization": (organization, "any"),
            "project": (project, "any"),
            "funding": (funding, funding_field),
            "variable": (variable, variable_field),
            "taxon": (taxon, taxon_rank),
        }
        q += [solr.any_field(FIELD_FILTERS[k][mode], v) for k, (v, mode) in given.items() if v]
        if license:
            value = license if ("*" in license or "?" in license) else f"*{license}*"
            q.append(solr.any_field(LICENSE_FIELDS, value))
        if concept_uri:
            q.append(ontology.annotation_clause([concept_uri]))
        has_text = bool(q)
        if owner:
            q.append(solr.any_field(OWNER_FIELDS[owner_role], owner))
        q += solr.coverage_clauses(north, south, east, west, start_date, end_date, spatial_mode)
        if record_date_from or record_date_to:
            q.append(solr.date_range(record_date_field, record_date_from, record_date_to))
        if sort is None:
            sort = "relevance" if has_text else "newest"
        out = await run(q, rows=rows, start=start, sort=sort, latest_only=latest_only)
        if concept_uri and (concept := ontology.concept_by_uri(concept_uri)):
            out = {"concept": {"uri": concept.uri, "label": concept.label}, **out}
        return out

    @mcp.tool(annotations=READ_ONLY, tags={"search", "provenance"})
    async def search_by_provenance(
        pid: Pid,
        relation: Annotated[
            Literal[tuple(PROV_FIELDS)],  # type: ignore[valid-type]
            Field(
                description="Provenance relation, read as '<result> <relation> <pid>', e.g. was_derived_from = objects derived from pid."
            ),
        ] = "was_derived_from",
        rows: Rows = 10,
        start: Start = 0,
    ) -> dict[str, Any]:
        """Find objects linked to a PID by a PROV relationship (derivations, sources, programs, executions).

        Example: search_by_provenance(pid="urn:uuid:...", relation="has_derivations")
        """
        return await run(
            f"{PROV_FIELDS[relation]}:{solr.phrase(pid)}",
            rows=rows,
            start=start,
            format_type=None,
            latest_only=False,
            fl=[*solr.METADATA_FIELDS, "fileName", "formatType", "size"],
        )

    @mcp.tool(annotations=READ_ONLY, tags={"search", "files"})
    async def search_data_files(
        file_name: Annotated[
            str | None, Field(description="File name or wildcard pattern, e.g. '*.nc', 'ODEN-*'.")
        ] = None,
        format_id: Annotated[
            str | None, Field(description="DataONE format ID, e.g. 'text/csv', 'netCDF-4', 'image/tiff'.")
        ] = None,
        min_size: Annotated[int | None, Field(ge=0, description="Minimum size in bytes.")] = None,
        max_size: Annotated[int | None, Field(ge=0, description="Maximum size in bytes.")] = None,
        documented_by: Annotated[str | None, Field(description="Only files documented by this metadata PID.")] = None,
        owner: Annotated[
            str | None,
            Field(description="Rights holder or submitter ORCID, e.g. 'http://orcid.org/0000-0003-1846-5140'."),
        ] = None,
        owner_role: OwnerRole = "either",
        format_type: Annotated[
            Literal["DATA", "METADATA", "RESOURCE", "any"],
            Field(description="Object type: data files (default), metadata records (e.g. EML), resource maps, or any."),
        ] = "DATA",
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "newest",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Search individual objects (not datasets) by file name, format, size or owner. Returns download URLs.
        Data files by default; format_type='METADATA' finds metadata records by format (e.g. one EML version).

        Example: search_data_files(file_name="*.nc", max_size=50000000)
        """
        q: list[str] = []
        if file_name:
            q.append(f"fileName:{solr.term(file_name)}")
        if format_id:
            q.append(f"formatId:{solr.phrase(format_id)}")
        if min_size is not None or max_size is not None:
            q.append(
                f"size:[{min_size if min_size is not None else '*'} TO {max_size if max_size is not None else '*'}]"
            )
        if documented_by:
            q.append(f"isDocumentedBy:{solr.phrase(documented_by)}")
        if owner:
            q.append(solr.any_field(OWNER_FIELDS[owner_role], owner))
        if not q:
            raise ToolError("Provide at least one filter")
        return await run(
            q,
            rows=rows,
            start=start,
            sort=sort,
            latest_only=latest_only,
            format_type=None if format_type == "any" else format_type,
            fl=None if format_type == "DATA" else [*solr.DATA_FIELDS, "title"],
        )

    @mcp.tool(annotations=READ_ONLY, tags={"search"})
    async def find_dataset_by_identifier(
        identifier: Annotated[
            str,
            Field(
                description="A DOI ('doi:10.18739/A2...' or 'https://doi.org/10.18739/...'), PID, series ID, or a data file PID."
            ),
        ],
    ) -> dict[str, Any]:
        """Resolve an identifier to its dataset(s): matches the PID, series ID, alternate identifiers,
        or—if a data-file PID is given—the dataset(s) that document it.

        Example: find_dataset_by_identifier(identifier="https://doi.org/10.18739/A29K4B")
        """
        ident = identifier.strip()
        for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/"):
            if ident.lower().startswith(prefix):
                ident = "doi:" + ident[len(prefix) :]
        p = solr.phrase(ident)
        return await run(
            f"id:{p} OR seriesId:{p} OR identifier:{p} OR documents:{p}",
            rows=20,
            latest_only=False,
        )

    @mcp.tool(annotations=READ_ONLY, tags={"search", "facets"})
    async def get_facet_counts(
        field: Annotated[
            str,
            Field(
                description="Solr field to facet on, e.g. 'keywords', 'formatId', 'project', 'funding', 'licenseName', 'authorLastName', 'placeKey', 'sem_annotation'."
            ),
        ],
        query: Annotated[
            str | None, Field(description="Optional Solr/free-text query restricting the documents counted.")
        ] = None,
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
            ("q", query or "*:*"),
            ("rows", 0),
            ("facet", "true"),
            ("facet.field", field),
            ("facet.limit", limit),
            ("facet.mincount", 1),
            ("facet.sort", "count"),
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
        name_contains: Annotated[
            str | None, Field(description="Optional case-insensitive filter on field name.")
        ] = None,
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
            fields.append(
                {
                    "name": name,
                    "type": f.get("type"),
                    "description": (f.get("description") or "").strip() or None,
                    "searchable": f.get("searchable") == "true",
                    "returnable": f.get("returnable") == "true",
                    "multivalued": f.get("multivalued") == "true",
                }
            )
        return {
            "engine": desc.get("name"),
            "version": desc.get("queryEngineVersion"),
            "count": len(fields),
            "fields": fields,
        }

    @mcp.tool(annotations=READ_ONLY, tags={"search", "advanced"})
    async def advanced_solr_query(
        q: Annotated[
            str,
            Field(
                description="Solr q parameter, e.g. 'keywords:permafrost AND beginDate:[2010-01-01T00:00:00Z TO *]'."
            ),
        ],
        fq: Annotated[
            list[str] | None, Field(description="Filter queries, e.g. ['formatType:METADATA', '-obsoletedBy:*'].")
        ] = None,
        fl: Annotated[
            str | None,
            Field(description="Comma-separated fields to return (see list_search_fields). Default: summary fields."),
        ] = None,
        sort: Annotated[str | None, Field(description="Solr sort, e.g. 'dateUploaded desc'.")] = None,
        rows: Rows = 10,
        start: Start = 0,
        facet_fields: Annotated[list[str] | None, Field(description="Optional fields to facet on.")] = None,
    ) -> dict[str, Any]:
        """Run a raw Solr query against the Arctic Data Center index, with full control over q/fq/fl/sort/facets.

        Returns raw Solr documents. Only public content is visible.
        """
        params: list[tuple[str, Any]] = [
            ("q", q),
            ("rows", min(rows, solr.MAX_ROWS)),
            ("start", start),
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
        out: dict[str, Any] = {
            "numFound": resp.get("numFound"),
            "start": resp.get("start"),
            "docs": resp.get("docs", []),
        }
        if facet_fields:
            ff = data.get("facet_counts", {}).get("facet_fields", {})
            out["facets"] = {
                k: [{"value": v[i], "count": v[i + 1]} for i in range(0, len(v) - 1, 2)] for k, v in ff.items()
            }
        return out
