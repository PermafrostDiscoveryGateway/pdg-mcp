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


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    client = ctx.client

    async def run(q: str | list[str], **kw: Any) -> dict[str, Any]:
        return await solr.search(client, q, **kw)

    @mcp.tool(annotations=READ_ONLY, tags={"search"})
    async def search_datasets(
        query: Annotated[
            str,
            Field(
                description="Free-text query, e.g. 'permafrost thaw methane'. Solr syntax (AND/OR/quotes) is allowed."
            ),
        ],
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "relevance",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Full-text search across dataset metadata (title, abstract, keywords, people, places, etc.).

        Best starting point for discovering Arctic Data Center datasets.
        Example: search_datasets(query="sea ice thickness Beaufort")
        """
        return await run(query, rows=rows, start=start, sort=sort, latest_only=latest_only)

    @mcp.tool(annotations=READ_ONLY, tags={"search"})
    async def search_by_title(
        title: Annotated[str, Field(description="Title phrase, or wildcard pattern such as '*snow depth*'.")],
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "relevance",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Find datasets whose title contains a phrase (or matches a wildcard pattern).

        Example: search_by_title(title="ice wedge")
        """
        return await run(f"title:{solr.term(title)}", rows=rows, start=start, sort=sort, latest_only=latest_only)

    @mcp.tool(annotations=READ_ONLY, tags={"search"})
    async def search_by_author(
        name: Annotated[
            str, Field(description="Creator/author name, e.g. 'Schuur' or 'Edward Schuur'. Wildcards allowed.")
        ],
        role: Annotated[
            Literal["any", "creator", "last_name", "investigator"],
            Field(
                description="'creator' = dataset origin/authors, 'last_name' = author surname only, 'investigator' = PI/associated party."
            ),
        ] = "any",
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "relevance",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Find datasets by author, creator, or investigator name.

        Example: search_by_author(name="Romanovsky", role="last_name")
        """
        fields = {
            "any": ["originText", "author", "authorLastName", "investigatorText"],
            "creator": ["originText", "author"],
            "last_name": ["authorLastName", "authorSurName"],
            "investigator": ["investigatorText"],
        }[role]
        return await run(solr.any_field(fields, name), rows=rows, start=start, sort=sort, latest_only=latest_only)

    @mcp.tool(annotations=READ_ONLY, tags={"search"})
    async def search_by_keyword(
        keyword: Annotated[str, Field(description="Keyword, e.g. 'permafrost', 'EARTH SCIENCE > CRYOSPHERE'.")],
        vocabulary: Annotated[
            Literal["any", "author_keywords", "gcmd", "topic"],
            Field(description="Restrict to author-supplied keywords, GCMD science keywords, or topic categories."),
        ] = "any",
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "relevance",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Find datasets tagged with a keyword (author keywords, GCMD keywords, or topic).

        Tip: use get_facet_counts(field="keywords") to discover popular keywords.
        Example: search_by_keyword(keyword="active layer")
        """
        fields = {
            "any": ["keywordsText", "gcmdKeyword", "topicText"],
            "author_keywords": ["keywordsText"],
            "gcmd": ["gcmdKeyword"],
            "topic": ["topicText"],
        }[vocabulary]
        return await run(solr.any_field(fields, keyword), rows=rows, start=start, sort=sort, latest_only=latest_only)

    @mcp.tool(annotations=READ_ONLY, tags={"search", "spatial"}, app=AppConfig(resource_uri=SEARCH_MAP_URI))
    async def search_by_bounding_box(
        north: Lat,
        south: Lat,
        east: Lon,
        west: Lon,
        mode: Annotated[
            Literal["intersects", "within"],
            Field(
                description="'intersects' = dataset extent overlaps the box; 'within' = dataset extent lies fully inside it."
            ),
        ] = "intersects",
        text: Annotated[
            str | None, Field(description="Optional free-text filter to combine with the spatial filter.")
        ] = None,
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "relevance",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Find datasets whose geographic coverage overlaps (or lies within) a lat/lon bounding box.

        Coordinates in decimal degrees (WGS84). Boxes crossing the antimeridian are not supported;
        split them into two queries.
        Example (North Slope of Alaska): search_by_bounding_box(north=71.5, south=68, east=-141, west=-166)
        """
        q = [solr.bbox_clause(north, south, east, west, mode)]
        if text:
            q.append(text)
        return await run(q, rows=rows, start=start, sort=sort, latest_only=latest_only)

    @mcp.tool(annotations=READ_ONLY, tags={"search", "temporal"})
    async def search_by_temporal_coverage(
        start_date: DateStr = None,
        end_date: DateStr = None,
        text: Annotated[str | None, Field(description="Optional free-text filter.")] = None,
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "relevance",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Find datasets whose data collection period overlaps a date range (beginDate/endDate).

        Leave start_date or end_date empty for an open-ended range.
        Example: search_by_temporal_coverage(start_date="2007", end_date="2009", text="caribou")
        """
        if not start_date and not end_date:
            raise ToolError("Provide at least one of start_date or end_date")
        q = [
            f"beginDate:[* TO {solr.solr_date(end_date, end_of_period=True)}]",
            f"endDate:[{solr.solr_date(start_date)} TO *]",
        ]
        if text:
            q.append(text)
        return await run(q, rows=rows, start=start, sort=sort, latest_only=latest_only)

    @mcp.tool(annotations=READ_ONLY, tags={"search", "temporal"})
    async def search_by_publication_date(
        start_date: DateStr = None,
        end_date: DateStr = None,
        date_field: Annotated[
            Literal["datePublished", "dateUploaded", "dateModified"],
            Field(description="Which date to filter on."),
        ] = "dateUploaded",
        text: Annotated[str | None, Field(description="Optional free-text filter.")] = None,
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "newest",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Find datasets published/uploaded/modified within a date range. Useful for "what's new".

        Example: search_by_publication_date(start_date="NOW-30DAYS")
        """
        q = [solr.date_range(date_field, start_date, end_date)]
        if text:
            q.append(text)
        return await run(q, rows=rows, start=start, sort=sort, latest_only=latest_only)

    @mcp.tool(annotations=READ_ONLY, tags={"search"})
    async def search_by_funding(
        award: Annotated[
            str,
            Field(
                description="Award number (e.g. '1503559'), funder name (e.g. 'National Science Foundation') or award title text."
            ),
        ],
        field: Annotated[
            Literal["any", "award_number", "funder_name", "award_title"],
            Field(description="Which funding field to search."),
        ] = "any",
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "relevance",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Find datasets produced under a funding award (e.g. an NSF Arctic award number) or funder.

        Example: search_by_funding(award="1503559", field="award_number")
        """
        fields = {
            "any": ["awardNumber", "fundingText", "funderName", "awardTitle"],
            "award_number": ["awardNumber", "fundingText"],
            "funder_name": ["funderName"],
            "award_title": ["awardTitle"],
        }[field]
        return await run(solr.any_field(fields, award), rows=rows, start=start, sort=sort, latest_only=latest_only)

    @mcp.tool(annotations=READ_ONLY, tags={"search"})
    async def search_by_project(
        project: Annotated[
            str, Field(description="Project name, e.g. 'Arctic Observing Network' or 'NGEE Arctic'. Wildcards allowed.")
        ],
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "relevance",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Find datasets that belong to a named research project.

        Tip: get_facet_counts(field="project") lists project names.
        Example: search_by_project(project="Arctic Observing Network")
        """
        return await run(
            solr.any_field(["projectText", "project"], project),
            rows=rows,
            start=start,
            sort=sort,
            latest_only=latest_only,
        )

    @mcp.tool(annotations=READ_ONLY, tags={"search"})
    async def search_by_measured_variable(
        variable: Annotated[
            str,
            Field(
                description="Variable/attribute name, label, description or unit, e.g. 'soil temperature', 'celsius', 'CH4'."
            ),
        ],
        field: Annotated[
            Literal["any", "name", "label", "description", "unit"],
            Field(description="Which attribute field to match."),
        ] = "any",
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "relevance",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Free-text search of EML data-table attributes by name, label, description or unit.

        For "datasets that measure X" questions prefer search_by_variable, which also finds datasets whose
        attributes are annotated with the matching ontology concept but described in other words.
        Use this tool for units, column names, or exact attribute wording.
        Example: search_by_measured_variable(variable="celsius", field="unit")
        """
        fields = {
            "any": solr.ATTRIBUTE_FIELDS,
            "name": ["attributeName"],
            "label": ["attributeLabel"],
            "description": ["attributeDescription"],
            "unit": ["attributeUnit"],
        }[field]
        return await run(solr.any_field(fields, variable), rows=rows, start=start, sort=sort, latest_only=latest_only)

    @mcp.tool(annotations=READ_ONLY, tags={"search"})
    async def search_by_taxon(
        taxon: Annotated[
            str, Field(description="Scientific name or taxon, e.g. 'Rangifer tarandus', 'Salix', 'Mammalia'.")
        ],
        rank: Annotated[
            Literal["any", "scientificName", "kingdom", "phylum", "class", "order", "family", "genus", "species"],
            Field(description="Taxonomic rank to match."),
        ] = "any",
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "relevance",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Find datasets with taxonomic coverage of an organism or taxon.

        Example: search_by_taxon(taxon="Ursus maritimus")
        """
        fields = (
            ["scientificName", "kingdom", "phylum", "class", "order", "family", "genus", "species"]
            if rank == "any"
            else [rank]
        )
        return await run(solr.any_field(fields, taxon), rows=rows, start=start, sort=sort, latest_only=latest_only)

    @mcp.tool(annotations=READ_ONLY, tags={"search", "semantic"})
    async def search_by_semantic_annotation(
        concept_uri: Annotated[
            str,
            Field(
                description="Ontology concept URI, e.g. 'http://purl.dataone.org/odo/ECSO_00001207' (ECSO 'Soil Depth'), a bare ECSO id such as 'ECSO_00001207', or a wildcard."
            ),
        ],
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "relevance",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Find datasets semantically annotated with an ontology concept (e.g. ECSO measurement types).

        DataONE PURLs are matched in both their http:// and https:// forms.
        Don't know the URI? find_variable_concepts maps a term like "soil depth" to ECSO PURLs, and
        search_by_variable does the lookup and search in one call.
        """
        out = await run(
            ontology.annotation_clause([concept_uri]), rows=rows, start=start, sort=sort, latest_only=latest_only
        )
        if concept := ontology.concept_by_uri(concept_uri):
            out = {"concept": {"uri": concept.uri, "label": concept.label}, **out}
        return out

    @mcp.tool(annotations=READ_ONLY, tags={"search", "spatial"})
    async def search_by_location_name(
        place: Annotated[str, Field(description="Place name, e.g. 'Toolik', 'Svalbard', 'Utqiagvik', 'Greenland'.")],
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "relevance",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Find datasets by named geographic place/site in their metadata (place keywords, site, geographic description).

        Example: search_by_location_name(place="Toolik Lake")
        """
        return await run(
            solr.any_field(["placeKey", "siteText", "namedLocation"], place),
            rows=rows,
            start=start,
            sort=sort,
            latest_only=latest_only,
        )

    @mcp.tool(annotations=READ_ONLY, tags={"search"})
    async def search_by_organization(
        organization: Annotated[str, Field(description="Organization name, e.g. 'University of Alaska Fairbanks'.")],
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "relevance",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Find datasets by contact or related organization.

        Example: search_by_organization(organization="University of Alaska Fairbanks")
        """
        return await run(
            solr.any_field(["contactOrganizationText", "relatedOrganizations"], organization),
            rows=rows,
            start=start,
            sort=sort,
            latest_only=latest_only,
        )

    @mcp.tool(annotations=READ_ONLY, tags={"search"})
    async def search_by_license(
        license: Annotated[
            str,
            Field(
                description="License name fragment (case-sensitive), e.g. 'Creative Commons Zero', 'Attribution', or an exact name from get_facet_counts(field='licenseName')."
            ),
        ],
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "relevance",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Find datasets whose indexed license name matches a value.

        Note: only a subset of records carry an indexed license; run
        get_facet_counts(field="licenseName") to see which license names exist.
        Example: search_by_license(license="Creative Commons Zero")
        """
        value = license if ("*" in license or "?" in license) else f"*{license}*"
        return await run(
            f"licenseName:{solr.term(value)} OR licenseUrl:{solr.term(value)}",
            rows=rows,
            start=start,
            sort=sort,
            latest_only=latest_only,
        )

    @mcp.tool(annotations=READ_ONLY, tags={"search"})
    async def search_by_rights_holder(
        subject: Annotated[
            str,
            Field(description="DataONE subject, usually an ORCID URL, e.g. 'http://orcid.org/0000-0002-1825-0097'."),
        ],
        role: Annotated[
            Literal["rights_holder", "submitter", "either"],
            Field(description="Match the object's owner (rightsHolder), its submitter, or either."),
        ] = "either",
        format_type: Annotated[
            Literal["METADATA", "DATA", "RESOURCE", "any"],
            Field(description="Kind of object to return."),
        ] = "METADATA",
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "newest",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Find objects owned or submitted by a DataONE subject (ORCID).

        Example: search_by_rights_holder(subject="http://orcid.org/0000-0003-1846-5140")
        """
        fields = {
            "rights_holder": ["rightsHolder"],
            "submitter": ["submitter"],
            "either": ["rightsHolder", "submitter"],
        }[role]
        return await run(
            solr.any_field(fields, subject),
            rows=rows,
            start=start,
            sort=sort,
            latest_only=latest_only,
            format_type=None if format_type == "any" else format_type,
        )

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
            q.append(
                f"size:[{min_size if min_size is not None else '*'} TO {max_size if max_size is not None else '*'}]"
            )
        if documented_by:
            q.append(f"isDocumentedBy:{solr.phrase(documented_by)}")
        if not q:
            raise ToolError("Provide at least one filter")
        return await run(q, rows=rows, start=start, sort=sort, latest_only=latest_only, format_type="DATA")

    @mcp.tool(annotations=READ_ONLY, tags={"search", "files"})
    async def search_by_format(
        format_id: Annotated[
            str,
            Field(
                description="DataONE format ID, e.g. 'text/csv', 'netCDF-4', 'https://eml.ecoinformatics.org/eml-2.2.0'."
            ),
        ],
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "newest",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """List objects of any type (data, metadata, resource maps) with a given DataONE format ID.

        Tip: list_object_formats() shows valid format IDs; get_facet_counts(field="formatId") shows which are used.
        """
        return await run(
            f"formatId:{solr.phrase(format_id)}",
            rows=rows,
            start=start,
            sort=sort,
            latest_only=latest_only,
            format_type=None,
            fl=[*solr.DATA_FIELDS, "title"],
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

    @mcp.tool(annotations=READ_ONLY, tags={"search"})
    async def count_datasets(
        text: Annotated[str | None, Field(description="Optional free-text query.")] = None,
        keyword: Annotated[str | None, Field(description="Optional keyword filter.")] = None,
        author: Annotated[str | None, Field(description="Optional author filter.")] = None,
        start_date: DateStr = None,
        end_date: DateStr = None,
        format_type: Annotated[
            Literal["METADATA", "DATA", "RESOURCE", "any"],
            Field(description="METADATA = datasets, DATA = data files, RESOURCE = packages."),
        ] = "METADATA",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Count matching objects without returning them (temporal filter uses data coverage dates).

        Example: count_datasets(keyword="permafrost", start_date="2015")
        """
        q: list[str] = []
        if text:
            q.append(text)
        if keyword:
            q.append(solr.any_field(["keywordsText", "gcmdKeyword"], keyword))
        if author:
            q.append(solr.any_field(["originText", "author"], author))
        if start_date or end_date:
            q.append(
                f"beginDate:[* TO {solr.solr_date(end_date, True)}] AND endDate:[{solr.solr_date(start_date)} TO *]"
            )
        res = await run(q, rows=0, latest_only=latest_only, format_type=None if format_type == "any" else format_type)
        return {"count": res["numFound"], "latest_only": latest_only, "query": res["query"]}

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
