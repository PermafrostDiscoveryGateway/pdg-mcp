"""Export ECSO measurement-type concepts into the small JSON lookup table the variable tools serve.

ECSO (the Ecosystem Ontology) is what Arctic Data Center attribute annotations point at, e.g.
http://purl.dataone.org/odo/ECSO_00001207 = "Soil Depth". This reads the published OWL file and keeps,
for each non-deprecated ECSO class, its label, synonyms, a short definition and its parents, so the
server can map a natural-language variable name to a concept PURL without calling an external service.

The ECSO repo has no tags or releases, so the upstream commit is recorded as the version. To export the
latest commit that touched the file:

    SHA=$(gh api 'repos/DataONEorg/sem-prov-ontologies/commits?path=ecso/ECSO8.owl&per_page=1' --jq '.[0].sha')
    curl -sLO "https://raw.githubusercontent.com/DataONEorg/sem-prov-ontologies/$SHA/ecso/ECSO8.owl"
    uv run python scripts/export_ecso_terms.py ECSO8.owl src/arcticdata_mcp/data/ecso/concepts.json --commit "$SHA"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from defusedxml import ElementTree as ET

NS = {
    "owl": "http://www.w3.org/2002/07/owl#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "obo": "http://purl.obolibrary.org/obo/",
    "oboInOwl": "http://www.geneontology.org/formats/oboInOwl#",
}
REPOSITORY = "DataONEorg/sem-prov-ontologies"
OWL_PATH = "ecso/ECSO8.owl"
ODO_HTTP = "http://purl.dataone.org/odo/"
# ECSO IRIs appear as http://, https:// and http://www.purl.dataone.org/odo/; all are written in the http:// form.
ECSO_IRI = re.compile(r"^https?://(?:www\.)?purl\.dataone\.org/odo/(ECSO_.*)$")
ECSO_ID = re.compile(r"^ECSO_\d{8}$")
RDF_ABOUT = f"{{{NS['rdf']}}}about"
RDF_RESOURCE = f"{{{NS['rdf']}}}resource"
LABEL_TAGS = ["rdfs:label", "skos:prefLabel"]
# Same meaning as the label: treated as an exact match.
EXACT_SYNONYM_TAGS = ["skos:altLabel", "skos:hiddenLabel", "oboInOwl:hasExactSynonym"]
# Broader, narrower or related terms: kept for lookup, but scored lower.
RELATED_SYNONYM_TAGS = ["oboInOwl:hasRelatedSynonym", "oboInOwl:hasBroadSynonym", "oboInOwl:hasNarrowSynonym"]
DEFINITION_TAG = "obo:IAO_0000115"
DEFINITION_CHARS = 300


def _texts(el, tags: list[str]) -> list[str]:
    out: list[str] = []
    for tag in tags:
        for child in el.findall(tag, NS):
            text = " ".join((child.text or "").split())
            if text and text not in out:
                out.append(text)
    return out


def canonical_ecso(iri: str) -> str | None:
    """http://purl.dataone.org/odo/ECSO_######## form of an ECSO class IRI, or None if it is not one."""
    m = ECSO_IRI.match(iri or "")
    return f"{ODO_HTTP}{m.group(1)}" if m and ECSO_ID.match(m.group(1)) else None


def export(owl_path: Path) -> list[dict]:
    root = ET.parse(owl_path).getroot()
    concepts = []
    for cls in root.findall("owl:Class", NS):
        about = cls.get(RDF_ABOUT, "")
        if not ECSO_IRI.match(about):
            continue
        uri = canonical_ecso(about)
        if uri is None:
            print(f"Skipping malformed ECSO IRI: {about}", file=sys.stderr)
            continue
        if (cls.findtext("owl:deprecated", "", NS) or "").strip().lower() == "true":
            continue
        labels = _texts(cls, LABEL_TAGS)
        if not labels:
            continue
        label = labels[0]
        exact = list(dict.fromkeys(
            s for s in _texts(cls, EXACT_SYNONYM_TAGS) + labels[1:] if s.lower() != label.lower()
        ))
        seen = {label.lower(), *(s.lower() for s in exact)}
        related = [s for s in _texts(cls, RELATED_SYNONYM_TAGS) if s.lower() not in seen]
        definition = " ".join((cls.findtext(DEFINITION_TAG, "", NS) or "").split())
        if len(definition) > DEFINITION_CHARS:
            definition = definition[:DEFINITION_CHARS].rsplit(" ", 1)[0] + "…"
        parents = list(dict.fromkeys(
            parent for p in cls.findall("rdfs:subClassOf", NS) if (parent := canonical_ecso(p.get(RDF_RESOURCE)))
        ))
        concept = {"uri": uri, "label": label}
        if exact:
            concept["synonyms"] = exact
        if related:
            concept["related_synonyms"] = related
        if definition:
            concept["definition"] = definition
        if parents:
            concept["parents"] = parents
        concepts.append(concept)
    return sorted(concepts, key=lambda c: c["uri"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("owl", type=Path, help="ECSO8.owl")
    parser.add_argument("out", type=Path, help="concepts.json to write")
    parser.add_argument("--commit", required=True, help=f"{REPOSITORY} commit SHA the OWL file was downloaded from")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        parser.error("--commit must be a full 40-character commit SHA")
    concepts = export(args.owl)
    source = {
        "repository": REPOSITORY,
        "path": OWL_PATH,
        "commit": args.commit,
        "url": f"https://github.com/{REPOSITORY}/blob/{args.commit}/{OWL_PATH}",
        "exported": datetime.now(UTC).date().isoformat(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"source": source, "concepts": concepts}, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"Wrote {len(concepts)} concepts to {args.out}")


if __name__ == "__main__":
    main()
