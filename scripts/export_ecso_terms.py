"""Export ECSO measurement-type concepts into the small JSON lookup table the variable tools serve.

ECSO (the Ecosystem Ontology) is what Arctic Data Center attribute annotations point at, e.g.
http://purl.dataone.org/odo/ECSO_00001207 = "Soil Depth". This reads the published OWL file and keeps,
for each non-deprecated ECSO class, its label, synonyms, a short definition and its parents, so the
server can map a natural-language variable name to a concept PURL without calling an external service.

concepts.json is generated, never edited by hand (#37). The ECSO repo has no tags or releases, so the
upstream commit and file are recorded in its "source" block as the version. Three commands:

    # What the ecso-refresh workflow runs weekly: export the highest-numbered ecso/ECSOn.owl at the latest
    # commit that touched it. The file is only rewritten when the concepts or the source change.
    uv run python scripts/export_ecso_terms.py latest

    # What CI runs on every pull request: re-export from the commit recorded in concepts.json and fail if
    # the committed file differs (the export date is ignored).
    uv run python scripts/export_ecso_terms.py check

    # Export a local copy of the OWL file, e.g. to try a change before it is upstream.
    uv run python scripts/export_ecso_terms.py owl ECSO8.owl --commit "$SHA" --path ecso/ECSO8.owl

Set GITHUB_TOKEN (or GH_TOKEN) to avoid GitHub's unauthenticated API rate limit; `check` only downloads
the raw file and does not need it.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
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
ECSO_DIR = "ecso"
# Released ECSO files are ecso/ECSO<n>.owl; the highest n is the current one (ECSO8.owl as of v0.10.0).
OWL_NAME = re.compile(r"^ECSO(\d+)\.owl$")
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "src/arcticdata_mcp/data/ecso/concepts.json"
GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"
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


def export(owl) -> list[dict]:
    """Concepts from an ECSO OWL file (a path or a binary file object), sorted by URI."""
    root = ET.parse(owl).getroot()
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
        exact = list(
            dict.fromkeys(s for s in _texts(cls, EXACT_SYNONYM_TAGS) + labels[1:] if s.lower() != label.lower())
        )
        seen = {label.lower(), *(s.lower() for s in exact)}
        related = [s for s in _texts(cls, RELATED_SYNONYM_TAGS) if s.lower() not in seen]
        definition = " ".join((cls.findtext(DEFINITION_TAG, "", NS) or "").split())
        if len(definition) > DEFINITION_CHARS:
            definition = definition[:DEFINITION_CHARS].rsplit(" ", 1)[0] + "…"
        parents = list(
            dict.fromkeys(
                parent for p in cls.findall("rdfs:subClassOf", NS) if (parent := canonical_ecso(p.get(RDF_RESOURCE)))
            )
        )
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


def document(concepts: list[dict], commit: str, path: str, exported: str | None = None) -> dict:
    """The concepts.json content: the concepts plus where they were exported from."""
    source = {
        "repository": REPOSITORY,
        "path": path,
        "commit": commit,
        "url": f"https://github.com/{REPOSITORY}/blob/{commit}/{path}",
        "exported": exported or datetime.now(UTC).date().isoformat(),
    }
    return {"source": source, "concepts": concepts}


def _without_date(doc: dict) -> dict:
    return {**doc, "source": {k: v for k, v in doc.get("source", {}).items() if k != "exported"}}


def same_content(a: dict, b: dict) -> bool:
    """Whether two concepts.json documents match, ignoring the export date."""
    return _without_date(a) == _without_date(b)


def serialize(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n"


def read(out: Path) -> dict | None:
    return json.loads(out.read_text()) if out.exists() else None


def write_if_changed(out: Path, doc: dict) -> bool:
    """Write doc unless out already has the same content, so an unchanged export keeps its date. True if written."""
    if (old := read(out)) is not None and same_content(old, doc):
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(serialize(doc))
    return True


def pick_owl(names: list[str]) -> str:
    """The highest-numbered ECSO<n>.owl among file names."""
    numbered = [(int(m.group(1)), name) for name in names if (m := OWL_NAME.match(name))]
    if not numbered:
        raise SystemExit(f"No ECSO<n>.owl file in {REPOSITORY}/{ECSO_DIR}: {sorted(names)}")
    return max(numbered)[1]


def client() -> httpx.Client:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token := os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(headers=headers, timeout=60, follow_redirects=True)


def _api(http: httpx.Client, endpoint: str, **params):
    r = http.get(f"{GITHUB_API}/{endpoint}", params=params)
    r.raise_for_status()
    return r.json()


def latest_source(http: httpx.Client) -> tuple[str, str]:
    """(commit, path) of the current ECSO file: the highest-numbered ECSO<n>.owl at the latest commit touching
    ecso/, and the latest commit that touched that file, so the recorded commit only moves when the file does."""
    repo = f"repos/{REPOSITORY}"
    dir_commits = _api(http, f"{repo}/commits", path=ECSO_DIR, per_page=1)
    if not dir_commits:
        raise SystemExit(f"No commits touch {REPOSITORY}/{ECSO_DIR}")
    listing = _api(http, f"{repo}/contents/{ECSO_DIR}", ref=dir_commits[0]["sha"])
    path = f"{ECSO_DIR}/{pick_owl([e['name'] for e in listing if e.get('type') == 'file'])}"
    file_commits = _api(http, f"{repo}/commits", path=path, sha=dir_commits[0]["sha"], per_page=1)
    return file_commits[0]["sha"], path


def download(http: httpx.Client, commit: str, path: str) -> io.BytesIO:
    r = http.get(f"{GITHUB_RAW}/{REPOSITORY}/{commit}/{path}")
    r.raise_for_status()
    return io.BytesIO(r.content)


def diff_concepts(old: list[dict], new: list[dict]) -> dict[str, list[str]]:
    """URIs added, removed and changed between two concept lists."""
    before = {c["uri"]: c for c in old}
    after = {c["uri"]: c for c in new}
    return {
        "added": sorted(after.keys() - before.keys()),
        "removed": sorted(before.keys() - after.keys()),
        "changed": sorted(u for u in before.keys() & after.keys() if before[u] != after[u]),
    }


def describe(old: dict | None, new: dict, limit: int = 20) -> str:
    """Markdown summary of what an export changes, for the refresh pull request."""
    src = new["source"]
    lines = [f"Exported `{src['path']}` at {REPOSITORY}@{src['commit'][:12]}: {len(new['concepts'])} concepts."]
    if old is None:
        return "\n".join(lines) + "\n"
    prev = old.get("source", {})
    if (prev.get("commit"), prev.get("path")) != (src["commit"], src["path"]):
        lines.append(f"Previously `{prev.get('path')}` at {REPOSITORY}@{str(prev.get('commit'))[:12]}.")
    lines.append("")
    labels = {c["uri"]: c["label"] for c in old.get("concepts", []) + new["concepts"]}
    for kind, uris in diff_concepts(old.get("concepts", []), new["concepts"]).items():
        if not uris:
            continue
        lines.append(f"- {len(uris)} {kind}:")
        lines += [f"  - `{u.rsplit('/', 1)[-1]}` {labels[u]}" for u in uris[:limit]]
        if len(uris) > limit:
            lines.append(f"  - … and {len(uris) - limit} more")
    return "\n".join(lines) + "\n"


def cmd_latest(args) -> int:
    with client() as http:
        commit, path = latest_source(http)
        print(f"Latest ECSO file: {path} at {commit}")
        doc = document(export(download(http, commit, path)), commit, path)
    old = read(args.out)
    if args.summary:
        args.summary.write_text(describe(old, doc))
    if write_if_changed(args.out, doc):
        print(f"Wrote {len(doc['concepts'])} concepts to {args.out}")
    else:
        print(f"{args.out} is up to date")
    return 0


def cmd_check(args) -> int:
    committed = read(args.out)
    if committed is None:
        print(f"{args.out} does not exist", file=sys.stderr)
        return 1
    src = committed.get("source", {})
    commit, path = src.get("commit", ""), src.get("path", "")
    if src.get("repository") != REPOSITORY or not re.fullmatch(r"[0-9a-f]{40}", commit) or not path:
        print(f"{args.out} does not record a {REPOSITORY} commit and path: {src}", file=sys.stderr)
        return 1
    with client() as http:
        doc = document(export(download(http, commit, path)), commit, path, src.get("exported"))
    if same_content(committed, doc):
        print(f"{args.out} matches the export of {path} at {commit}")
        return 0
    d = diff_concepts(committed.get("concepts", []), doc["concepts"])
    wording = {"added": "missing from the file", "removed": "not in the export", "changed": "edited"}
    counts = ", ".join(f"{len(v)} {wording[k]}" for k, v in d.items() if v) or "source block differs"
    examples = [u.rsplit("/", 1)[-1] for v in d.values() for u in v][:10]
    print(
        f"{args.out} does not match the export of {path} at {commit} ({counts}; e.g. {', '.join(examples)}).\n"
        "It is generated: don't edit it by hand. Run the ecso-refresh workflow (Actions tab > ecso-refresh > Run "
        "workflow) or `uv run python scripts/export_ecso_terms.py latest` and commit the result.",
        file=sys.stderr,
    )
    return 1


def cmd_owl(args) -> int:
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        raise SystemExit("--commit must be a full 40-character commit SHA")
    doc = document(export(args.owl), args.commit, args.path)
    write_if_changed(args.out, doc)
    print(f"Wrote {len(doc['concepts'])} concepts to {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    out_help = f"concepts.json (default: {DEFAULT_OUT.relative_to(DEFAULT_OUT.parents[4])})"

    p = sub.add_parser("latest", help="export the current ECSO file from GitHub")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help=out_help)
    p.add_argument("--summary", type=Path, help="also write a Markdown summary of the changes here")
    p.set_defaults(func=cmd_latest)

    p = sub.add_parser("check", help="fail if concepts.json differs from the export of its recorded commit")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help=out_help)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("owl", help="export a local OWL file")
    p.add_argument("owl", type=Path, help="ECSO OWL file, e.g. ECSO8.owl")
    p.add_argument("--commit", required=True, help=f"{REPOSITORY} commit SHA the OWL file was downloaded from")
    p.add_argument("--path", default=f"{ECSO_DIR}/ECSO8.owl", help="path of the file in that repository")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help=out_help)
    p.set_defaults(func=cmd_owl)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
