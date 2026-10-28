"""Export ECSO measurement-type concepts into the small JSON lookup table the variable tools serve.

ECSO (the Ecosystem Ontology) is what Arctic Data Center attribute annotations point at, e.g.
http://purl.dataone.org/odo/ECSO_00001207 = "Soil Depth". This reads the published OWL file and keeps,
for each non-deprecated ECSO class, its label, synonyms, a short definition and its parents, so the
server can map a natural-language variable name to a concept PURL without calling an external service.

    curl -sLO https://raw.githubusercontent.com/DataONEorg/sem-prov-ontologies/main/ecso/ECSO8.owl
    uv run python scripts/export_ecso_terms.py ECSO8.owl src/arcticdata_mcp/data/ecso/concepts.json
"""

from __future__ import annotations

import json
import sys
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
ECSO_PREFIX = "http://purl.dataone.org/odo/ECSO_"
RDF_ABOUT = f"{{{NS['rdf']}}}about"
RDF_RESOURCE = f"{{{NS['rdf']}}}resource"
LABEL_TAGS = ["rdfs:label", "skos:prefLabel"]
SYNONYM_TAGS = [
    "skos:altLabel", "skos:hiddenLabel", "oboInOwl:hasExactSynonym",
    "oboInOwl:hasRelatedSynonym", "oboInOwl:hasBroadSynonym", "oboInOwl:hasNarrowSynonym",
]
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


def export(owl_path: Path) -> list[dict]:
    root = ET.parse(owl_path).getroot()
    concepts = []
    for cls in root.findall("owl:Class", NS):
        uri = cls.get(RDF_ABOUT, "")
        if not uri.startswith(ECSO_PREFIX):
            continue
        if (cls.findtext("owl:deprecated", "", NS) or "").strip().lower() == "true":
            continue
        labels = _texts(cls, LABEL_TAGS)
        if not labels:
            continue
        label = labels[0]
        synonyms = [s for s in _texts(cls, SYNONYM_TAGS) + labels[1:] if s.lower() != label.lower()]
        definition = " ".join((cls.findtext(DEFINITION_TAG, "", NS) or "").split())
        if len(definition) > DEFINITION_CHARS:
            definition = definition[:DEFINITION_CHARS].rsplit(" ", 1)[0] + "…"
        parents = [
            p.get(RDF_RESOURCE) for p in cls.findall("rdfs:subClassOf", NS)
            if (p.get(RDF_RESOURCE) or "").startswith(ECSO_PREFIX)
        ]
        concept = {"uri": uri, "label": label}
        if synonyms:
            concept["synonyms"] = list(dict.fromkeys(synonyms))
        if definition:
            concept["definition"] = definition
        if parents:
            concept["parents"] = parents
        concepts.append(concept)
    return sorted(concepts, key=lambda c: c["uri"])


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    concepts = export(Path(sys.argv[1]))
    out = Path(sys.argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"source": "ECSO8.owl", "concepts": concepts}, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"Wrote {len(concepts)} concepts to {out}")


if __name__ == "__main__":
    main()
