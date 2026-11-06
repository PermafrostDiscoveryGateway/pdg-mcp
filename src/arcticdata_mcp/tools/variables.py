"""Variable search by ontology concept: resolve "soil depth" to an ECSO PURL, then search sem_annotation.

Free-text attribute search only finds datasets whose authors used the same words; annotation search finds
every dataset whose attributes were annotated with the concept. These tools resolve the term first and
fall back to (or add) free text, labelling each result with how it matched.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from arcticdata_mcp import ontology, solr
from arcticdata_mcp.tools.common import READ_ONLY, LatestOnly, Rows, Sort, Start, ToolContext

Term = Annotated[str, Field(description="Variable in plain language, e.g. 'soil depth', 'active layer thickness', 'methane flux'.")]
MAX_AUTO_CONCEPTS = 5


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    client = ctx.client
    onto = ctx.settings.ontology

    async def resolve(term: str, limit: int) -> tuple[list[ontology.Match], str | None]:
        """Ranked concept matches, plus a warning if BioPortal was configured but failed."""
        local = ontology.match_local(term, limit)
        if not onto.bioportal_api_key:
            return local, None
        try:
            remote = await ontology.match_bioportal(client, term, onto.bioportal_api_key, onto.bioportal_url, limit)
        except (ToolError, ValueError) as exc:
            return local, f"BioPortal lookup failed, using the bundled ECSO table only: {exc}"
        return ontology.merge(local, remote)[:limit], None

    @mcp.tool(annotations=READ_ONLY, tags={"search", "semantic"})
    async def find_variable_concepts(
        term: Term,
        limit: Annotated[int, Field(ge=1, le=25, description="Maximum number of concepts.")] = 5,
        with_counts: Annotated[bool, Field(description="Also count the datasets annotated with each concept.")] = True,
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Map a natural-language variable name to ECSO ontology concepts (PURLs), ranked by match score.

        Use the returned uri with search_by_semantic_annotation, or pass several to search_by_variable
        (concept_uris=[...]). Scores: 1.0 = exact label or synonym, >=0.72 = all words match.
        dataset_count is the number of datasets annotated with the concept.
        Example: find_variable_concepts(term="soil depth")
        """
        matches, warning = await resolve(term, limit)
        concepts = [m.to_dict() for m in matches]
        if with_counts and concepts:
            counts = await ontology.dataset_counts(client, [c["uri"] for c in concepts], latest_only)
            for c in concepts:
                c["dataset_count"] = counts[c["uri"]]
            concepts.sort(key=lambda c: (-c["score"], -c["dataset_count"]))
        out: dict[str, Any] = {
            "term": term,
            "latest_only": latest_only,
            "bioportal": "used" if onto.bioportal_api_key and not warning else "not used",
            "concepts": concepts,
        }
        if not concepts:
            out["hint"] = "No ECSO concept matches; use search_by_measured_variable for a free-text attribute search."
        if warning:
            out["warning"] = warning
        return out

    @mcp.tool(annotations=READ_ONLY, tags={"search", "semantic"})
    async def search_by_variable(
        variable: Term,
        concept_uris: Annotated[
            list[str] | None,
            Field(description="Concept PURLs or ECSO ids to search instead of resolving `variable` automatically (e.g. picked from find_variable_concepts)."),
        ] = None,
        include_free_text: Annotated[
            bool,
            Field(description="Also include datasets whose attribute names/descriptions mention the variable but are not annotated."),
        ] = True,
        rows: Rows = 10,
        start: Start = 0,
        sort: Sort = "relevance",
        latest_only: LatestOnly = True,
    ) -> dict[str, Any]:
        """Best tool for "datasets that measure X": finds datasets by measured variable using ontology annotations.

        Resolves the variable to ECSO concept PURLs and searches the semantic annotations, which catches
        datasets described in other words. Annotated and free-text attribute matches are combined and
        de-duplicated; each result's match_type is "annotation", "free_text" or "both". If no concept
        matches well, it falls back to free text. counts gives the total per match type.
        Example: search_by_variable(variable="soil depth")
        """
        candidates: list[ontology.Match] = []
        warning = None
        if concept_uris:
            uris = list(dict.fromkeys(ontology.normalize_concept_id(u) for u in concept_uris))
            resolution = "explicit"
        else:
            candidates, warning = await resolve(variable, 10)
            uris = [m.concept.uri for m in ontology.top_matches(variable, candidates)][:MAX_AUTO_CONCEPTS]
            resolution = "concept" if uris else "free_text_fallback"

        annotation = ontology.annotation_clause(uris) if uris else None
        if uris:
            free_text = solr.any_field(solr.ATTRIBUTE_FIELDS, variable) if include_free_text else None
        else:
            # Without a concept, a phrase match is often empty ("permafrost temperature"), so match all words.
            free_text = "attribute:(" + " AND ".join(solr.term(w) for w in variable.split()) + ")"
        clauses = [c for c in (annotation, free_text) if c]
        q = clauses[0] if len(clauses) == 1 else " OR ".join(f"({c})" for c in clauses)
        facets: list[tuple[str, Any]] = [("facet", "true")]
        if annotation:
            facets.append(("facet.query", f"{{!key=annotation}}{annotation}"))
        if free_text:
            facets.append(("facet.query", f"{{!key=free_text}}{free_text}"))
        res = await solr.search(
            client, q, rows=rows, start=start, sort=sort, latest_only=latest_only,
            fl=[*solr.METADATA_FIELDS, "sem_annotation"], extra_params=facets, raw=True,
        )

        docs = res.pop("results")
        variants = {v for u in uris for v in ontology.purl_variants(u)}
        free_ids: set[str] = set()
        if annotation and free_text and docs:
            ids = " OR ".join(solr.phrase(d["id"]) for d in docs)
            check = await solr.search(client, [f"id:({ids})", free_text], rows=len(docs), latest_only=False, fl=["id"], raw=True)
            free_ids = {d["id"] for d in check["results"]}
        results = []
        for d in docs:
            annotated = bool(variants & set(d.get("sem_annotation") or []))
            text_match = (d["id"] in free_ids) if annotation else True
            r = solr.summarize(d, client)
            r["match_type"] = "both" if annotated and text_match else "annotation" if annotated else "free_text"
            results.append(r)

        counts = res.pop("facet_queries", {})
        used = []
        for u in uris:
            c = ontology.concept_by_uri(u)
            used.append({"uri": u, "label": c.label if c else None})
        out: dict[str, Any] = {
            "variable": variable,
            "resolution": resolution,
            "concepts_used": used,
            "numFound": res["numFound"],
            "counts": {"annotation": counts.get("annotation", 0), "free_text": counts.get("free_text", 0)},
            "latest_only": latest_only,
            "start": res["start"],
            "rows": res["rows"],
            "query": res["query"],
            "results": results,
        }
        others = [m for m in candidates if m.concept.uri not in uris][:5]
        if others:
            out["other_candidates"] = [{"uri": m.concept.uri, "label": m.concept.label, "score": round(m.score, 3)} for m in others]
        if resolution == "free_text_fallback":
            out["note"] = (
                "No ECSO concept matched well, so only a free-text search (all words in the attribute text) was run. "
                "Check other_candidates or find_variable_concepts for a concept to pass as concept_uris."
            )
        if warning:
            out["warning"] = warning
        return out
