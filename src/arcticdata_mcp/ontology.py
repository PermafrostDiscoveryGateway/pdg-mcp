"""Map natural-language variable names to ontology concept PURLs (ECSO) for semantic search.

Arctic Data Center attributes are annotated with ECSO measurement types, indexed in Solr as
``sem_annotation``. Searching on the concept PURL finds annotated datasets whatever words their authors
used, so it is more complete than free text (e.g. "soil depth": 34 free-text vs 122 PURL matches).
The lookup table is bundled (see scripts/export_ecso_terms.py); BioPortal is consulted as well when an
API key is configured.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import cache
from importlib.resources import files
from typing import Any

from fastmcp.exceptions import ToolError

from arcticdata_mcp import solr
from arcticdata_mcp.client import DataONEClient

ODO_HTTP = "http://purl.dataone.org/odo/"
ODO_HTTPS = "https://purl.dataone.org/odo/"
BIOPORTAL_ONTOLOGY = "ECSO"
# A concept scoring at least this well is used automatically by search_by_variable.
STRONG_MATCH = 0.72
MIN_SCORE = 0.3
# Related/broad/narrow synonyms are not equivalent names (ECSO lists "moisture" under Volumetric Water Content
# Method, "animal" under Multi-Celled Organism), so a match on one is capped below STRONG_MATCH: it is offered
# as a candidate but never used automatically.
RELATED_MAX_SCORE = 0.7

_ODO_ID = re.compile(r"^(?:(?:https?://(?:www\.)?purl\.dataone\.org/odo/)|(?:ecso:))?(ECSO_\d{8})$", re.IGNORECASE)
_NON_WORD = re.compile(r"[^a-z0-9]+")


@dataclass
class Concept:
    uri: str
    label: str
    synonyms: list[str] = field(default_factory=list)
    definition: str | None = None
    parents: list[str] = field(default_factory=list)
    related_synonyms: list[str] = field(default_factory=list)


@dataclass
class Match:
    concept: Concept
    score: float
    matched_on: str
    sources: list[str] = field(default_factory=lambda: ["ecso_table"])

    def to_dict(self) -> dict[str, Any]:
        c = self.concept
        out: dict[str, Any] = {"uri": c.uri, "label": c.label, "score": round(self.score, 3), "matched_on": self.matched_on}
        if self.matched_on == c.label:
            out["matched_as"] = "label"
        elif self.matched_on in c.synonyms:
            out["matched_as"] = "synonym"
        elif self.matched_on in c.related_synonyms:
            out["matched_as"] = "related_synonym"
        if c.synonyms:
            out["synonyms"] = c.synonyms
        if c.related_synonyms:
            out["related_synonyms"] = c.related_synonyms
        if c.definition:
            out["definition"] = c.definition
        out["source"] = self.sources
        return out


def normalize_concept_id(value: str) -> str:
    """Expand a bare ECSO id ('ECSO_00001207', 'ecso:ECSO_00001207') to its canonical PURL."""
    value = value.strip()
    m = _ODO_ID.match(value)
    return f"{ODO_HTTP}{m.group(1).upper()}" if m else value


def purl_variants(uri: str) -> list[str]:
    """All forms of a concept URI found in the index. DataONE PURLs appear as both http:// and https://."""
    uri = normalize_concept_id(uri)
    for a, b in ((ODO_HTTP, ODO_HTTPS), (ODO_HTTPS, ODO_HTTP)):
        if uri.startswith(a):
            return [uri, b + uri[len(a):]]
    return [uri]


def annotation_clause(uris: list[str]) -> str:
    """Solr clause matching any of ``uris`` (with http/https variants) in sem_annotation."""
    if not uris:
        raise ToolError("Provide at least one concept URI")
    values: list[str] = []
    for u in uris:
        if "*" in u or "?" in u:
            values.append(solr.term(u))
        else:
            values += [solr.phrase(v) for v in purl_variants(u)]
    return "sem_annotation:(" + " OR ".join(dict.fromkeys(values)) + ")"


def _tokens(text: str) -> list[str]:
    words = _NON_WORD.sub(" ", text.lower()).split()
    # Light stemming so "depths"/"temperatures" match the singular labels.
    return [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words]


def score_name(query: str, name: str) -> float:
    """Similarity of a query to one concept name in [0, 1]: 1 for an exact match, otherwise token overlap
    weighted towards covering every query word."""
    q, n = _tokens(query), _tokens(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    overlap = len(set(q) & set(n))
    if not overlap:
        return 0.0
    recall, precision = overlap / len(set(q)), overlap / len(set(n))
    return 0.9 * (0.65 * recall + 0.35 * precision)


def best_name(query: str, concept: Concept) -> tuple[float, str]:
    best = (0.0, concept.label)
    for name in [concept.label, *concept.synonyms]:
        s = score_name(query, name)
        if s > best[0]:
            best = (s, name)
    for name in concept.related_synonyms:
        s = min(score_name(query, name), RELATED_MAX_SCORE)
        if s > best[0]:
            best = (s, name)
    return best


@cache
def load_concepts() -> tuple[Concept, ...]:
    raw = json.loads(files("arcticdata_mcp").joinpath("data/ecso/concepts.json").read_text())
    return tuple(
        Concept(
            c["uri"], c["label"], c.get("synonyms", []), c.get("definition"), c.get("parents", []),
            related_synonyms=c.get("related_synonyms", []),
        )
        for c in raw["concepts"]
    )


@cache
def _concepts_by_uri() -> dict[str, Concept]:
    return {c.uri: c for c in load_concepts()}


def concept_by_uri(uri: str) -> Concept | None:
    """Bundled concept for a URI or bare ECSO id, in either http:// or https:// form."""
    uri = normalize_concept_id(uri)
    if uri.startswith(ODO_HTTPS):
        uri = ODO_HTTP + uri[len(ODO_HTTPS):]
    return _concepts_by_uri().get(uri)


def match_local(query: str, limit: int = 10) -> list[Match]:
    """Rank bundled ECSO concepts against a natural-language variable name."""
    if not query.strip():
        raise ToolError("Variable term must not be empty")
    matches = []
    for c in load_concepts():
        s, name = best_name(query, c)
        if s >= MIN_SCORE:
            matches.append(Match(c, s, name))
    # Prefer shorter labels on ties: "Soil Temperature" before "Average Layer Soil Temperature".
    matches.sort(key=lambda m: (-m.score, len(m.concept.label)))
    return matches[:limit]


async def match_bioportal(client: DataONEClient, query: str, api_key: str, url: str, limit: int = 10) -> list[Match]:
    """Search ECSO on BioPortal, which also matches definitions and related terms."""
    resp = await client.request(
        "GET", f"{url.rstrip('/')}/search",
        params={"q": query, "ontologies": BIOPORTAL_ONTOLOGY, "pagesize": limit, "include": "prefLabel,synonym,definition"},
        headers={"Authorization": f"apikey token={api_key}"},
    )
    out = []
    for item in resp.json().get("collection", []):
        uri = normalize_concept_id(item.get("@id", ""))
        local = concept_by_uri(uri)
        concept = local or Concept(
            uri, item.get("prefLabel") or uri, item.get("synonym") or [],
            " ".join(item.get("definition") or []) or None,
        )
        s, name = best_name(query, concept)
        # BioPortal found it for a reason (definition, subclass), so keep it above the noise floor.
        out.append(Match(concept, max(s, 0.5), name, ["bioportal"]))
    return out


def merge(*groups: list[Match]) -> list[Match]:
    by_uri: dict[str, Match] = {}
    for group in groups:
        for m in group:
            prev = by_uri.get(m.concept.uri)
            if prev is None:
                by_uri[m.concept.uri] = m
                continue
            prev.sources = list(dict.fromkeys(prev.sources + m.sources))
            if m.score > prev.score:
                prev.score, prev.matched_on = m.score, m.matched_on
    return sorted(by_uri.values(), key=lambda m: (-m.score, len(m.concept.label)))


def top_matches(query: str, matches: list[Match]) -> list[Match]:
    """Concepts to use automatically for ``query`` from ranked ``matches`` (best first); [] if none is strong.

    Takes the concepts tied for the top score. When some of those match through their own label rather than
    a synonym, only those are kept: "albedo" should use the concept labelled "albedo", not concepts that merely
    list "albedo" as a synonym. If no tied concept matches by label (e.g. a synonym-only match), all are kept.
    """
    if not matches or matches[0].score < STRONG_MATCH:
        return []
    tied = [m for m in matches if m.score == matches[0].score]
    by_label = [m for m in tied if score_name(query, m.concept.label) == 1.0]
    return by_label or tied


async def dataset_counts(client: DataONEClient, uris: list[str], latest_only: bool = True) -> dict[str, int]:
    """Number of datasets annotated with each concept, in a single Solr request."""
    if not uris:
        return {}
    params: list[tuple[str, Any]] = [("q", "*:*"), ("rows", 0), ("facet", "true"), ("fq", "formatType:METADATA")]
    if latest_only:
        params.append(("fq", "-obsoletedBy:*"))
    params += [("facet.query", f"{{!key=c{i}}}{annotation_clause([u])}") for i, u in enumerate(uris)]
    data = await client.solr(params)
    fq = data.get("facet_counts", {}).get("facet_queries", {})
    return {u: fq.get(f"c{i}", 0) for i, u in enumerate(uris)}
