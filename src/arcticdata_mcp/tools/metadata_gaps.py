"""Metadata-quality search: find datasets that lack a metadata field, to prioritise enrichment work."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from arcticdata_mcp import solr
from arcticdata_mcp.tools.common import READ_ONLY, LatestOnly, Rows, Sort, Start, ToolContext

# Keep in step with solr.PRESENT_CLAUSES (a test checks this).
MissingField = Literal[
    "attributes", "units", "keywords", "abstract", "spatial_coverage", "temporal_coverage", "funding",
    "semantic_annotations",
]  # fmt: skip


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    client = ctx.client

    @mcp.tool(annotations=READ_ONLY, tags={"search", "metadata-quality"})
    async def search_missing_metadata(
        missing: Annotated[
            list[MissingField],
            Field(
                min_length=1,
                description=(
                    "Metadata the dataset must lack: attributes (EML data-table attribute descriptions), units, "
                    "keywords, abstract, spatial_coverage (bounding box), temporal_coverage (dates), funding "
                    "(award number, funder or funding text), semantic_annotations (ontology terms)."
                ),
            ),
        ],
        match: Annotated[
            Literal["all", "any"],
            Field(description="'all': lacks every listed field. 'any': lacks at least one of them."),
        ] = "all",
        query: Annotated[
            str | None,
            Field(description="Optional free-text query to scope the search, e.g. 'permafrost'. Solr syntax is allowed."),
        ] = None,
        eml_only: Annotated[
            bool,
            Field(description="Only EML records. Portals and ISO, Dublin Core and FGDC records have no EML fields and would all show as missing."),
        ] = True,
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "newest",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Find datasets that lack metadata fields, to prioritize metadata enrichment.

        Returns the matching datasets (newest first by default) and `also_missing`: of all the matches, how many
        also lack each field, which shows where the gaps cluster. A dataset with no tabular data legitimately
        has no `attributes`, so combine with `query` or check results before treating that as a defect.
        License is not offered: only about 1% of records have an indexed license, so "missing" would be misleading.
        Example: search_missing_metadata(missing=["keywords", "spatial_coverage"], match="any", query="permafrost")
        """
        fq = solr.missing_filters(missing, match)
        if eml_only:
            fq.insert(0, solr.EML_FORMAT_FQ)
        res = await solr.search(
            client,
            [query] if query else [],
            rows=rows,
            start=start,
            sort=sort,
            latest_only=latest_only,
            extra_fq=fq,
            facet_queries={name: solr.missing_clause(name) for name in solr.PRESENT_CLAUSES},
        )
        also_missing = res.pop("facet_queries")
        return {
            "missing": missing,
            "match": match,
            "eml_only": eml_only,
            "numFound": res["numFound"],
            "also_missing": also_missing,
            "start": res["start"],
            "rows": res["rows"],
            "query": res["query"],
            "results": res["results"],
        }
