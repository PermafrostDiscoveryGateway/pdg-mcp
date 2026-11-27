"""scripts/export_ecso_terms.py: the export, the refresh (`latest`) and the no-hand-edits check (`check`), #37."""

import importlib.util
import io
import json
from pathlib import Path

import httpx
import pytest
import respx

_spec = importlib.util.spec_from_file_location(
    "export_ecso_terms", Path(__file__).resolve().parents[1] / "scripts" / "export_ecso_terms.py"
)
ecso = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ecso)

COMMIT = "a" * 40
NEWER = "b" * 40
ODO = "http://purl.dataone.org/odo/"

OWL = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#" xmlns:skos="http://www.w3.org/2004/02/skos/core#"
         xmlns:obo="http://purl.obolibrary.org/obo/">
  <owl:Class rdf:about="http://purl.dataone.org/odo/ECSO_00001207">
    <rdfs:label>Soil Depth</rdfs:label>
    <skos:altLabel>depth of soil</skos:altLabel>
    <obo:IAO_0000115>How deep.</obo:IAO_0000115>
  </owl:Class>
  <owl:Class rdf:about="http://www.purl.dataone.org/odo/ECSO_00010077">
    <rdfs:label>Water Content</rdfs:label>
    <rdfs:subClassOf rdf:resource="https://purl.dataone.org/odo/ECSO_00001207"/>
  </owl:Class>
  <owl:Class rdf:about="http://purl.dataone.org/odo/ECSO_#PAR"><rdfs:label>PAR</rdfs:label></owl:Class>
  <owl:Class rdf:about="http://purl.dataone.org/odo/ECSO_00000001">
    <rdfs:label>Old</rdfs:label><owl:deprecated>true</owl:deprecated>
  </owl:Class>
  <owl:Class rdf:about="http://purl.obolibrary.org/obo/ENVO_00002003"><rdfs:label>feces</rdfs:label></owl:Class>
</rdf:RDF>
"""


def _concepts():
    return ecso.export(io.BytesIO(OWL))


def test_export_normalises_www_and_skips_malformed_deprecated_and_foreign_classes():
    concepts = _concepts()
    assert [c["uri"] for c in concepts] == [f"{ODO}ECSO_00001207", f"{ODO}ECSO_00010077"]
    assert concepts[0]["synonyms"] == ["depth of soil"] and concepts[0]["definition"] == "How deep."
    assert concepts[1]["parents"] == [f"{ODO}ECSO_00001207"]


def test_document_records_the_source():
    src = ecso.document([], COMMIT, "ecso/ECSO8.owl", "2026-01-01")["source"]
    assert src == {
        "repository": "DataONEorg/sem-prov-ontologies",
        "path": "ecso/ECSO8.owl",
        "commit": COMMIT,
        "url": f"https://github.com/DataONEorg/sem-prov-ontologies/blob/{COMMIT}/ecso/ECSO8.owl",
        "exported": "2026-01-01",
    }


def test_unchanged_export_keeps_the_file_and_its_date(tmp_path):
    out = tmp_path / "concepts.json"
    assert ecso.write_if_changed(out, ecso.document(_concepts(), COMMIT, "ecso/ECSO8.owl", "2026-01-01"))
    assert not ecso.write_if_changed(out, ecso.document(_concepts(), COMMIT, "ecso/ECSO8.owl", "2026-02-02"))
    assert json.loads(out.read_text())["source"]["exported"] == "2026-01-01"
    assert ecso.write_if_changed(out, ecso.document(_concepts()[:1], COMMIT, "ecso/ECSO8.owl", "2026-02-02"))
    assert json.loads(out.read_text())["source"]["exported"] == "2026-02-02"


def test_pick_owl_takes_the_highest_number():
    assert ecso.pick_owl(["ECSO8.owl", "ECSO10.owl", "ECSO9.owl", "ECSO8.ttl", "README.md"]) == "ECSO10.owl"
    with pytest.raises(SystemExit):
        ecso.pick_owl(["README.md"])


def test_describe_lists_added_removed_and_changed():
    old = ecso.document(_concepts(), COMMIT, "ecso/ECSO8.owl")
    new_concepts = [{**_concepts()[0], "label": "Depth of Soil"}, {"uri": f"{ODO}ECSO_00009999", "label": "New"}]
    text = ecso.describe(old, ecso.document(new_concepts, NEWER, "ecso/ECSO9.owl"))
    assert "`ecso/ECSO9.owl` at DataONEorg/sem-prov-ontologies@bbbbbbbbbbbb" in text
    assert "Previously `ecso/ECSO8.owl`" in text
    assert "- 1 added:\n  - `ECSO_00009999` New" in text
    assert "- 1 removed:\n  - `ECSO_00010077` Water Content" in text
    assert "- 1 changed:\n  - `ECSO_00001207` Depth of Soil" in text


def _mock_github(file_commit=NEWER):
    api, raw = ecso.GITHUB_API, ecso.GITHUB_RAW
    repo = "DataONEorg/sem-prov-ontologies"
    respx.get(f"{api}/repos/{repo}/commits", params={"path": "ecso"}).respond(json=[{"sha": "c" * 40}])
    respx.get(f"{api}/repos/{repo}/contents/ecso").respond(
        json=[{"name": "ECSO8.owl", "type": "file"}, {"name": "ECSO9.owl", "type": "file"}]
    )
    respx.get(f"{api}/repos/{repo}/commits", params={"path": "ecso/ECSO9.owl"}).respond(json=[{"sha": file_commit}])
    respx.get(f"{raw}/{repo}/{file_commit}/ecso/ECSO9.owl").respond(content=OWL)
    respx.get(f"{raw}/{repo}/{COMMIT}/ecso/ECSO8.owl").respond(content=OWL)


@respx.mock
def test_latest_exports_the_newest_file_at_its_own_commit(tmp_path):
    _mock_github()
    out, summary = tmp_path / "concepts.json", tmp_path / "summary.md"
    assert ecso.main(["latest", "--out", str(out), "--summary", str(summary)]) == 0
    src = json.loads(out.read_text())["source"]
    assert (src["path"], src["commit"]) == ("ecso/ECSO9.owl", NEWER)
    assert "2 concepts" in summary.read_text()


@respx.mock
def test_check_passes_for_the_export_and_fails_for_a_hand_edit(tmp_path, capsys):
    _mock_github()
    out = tmp_path / "concepts.json"
    out.write_text(ecso.serialize(ecso.document(_concepts(), COMMIT, "ecso/ECSO8.owl", "2020-01-01")))
    assert ecso.main(["check", "--out", str(out)]) == 0

    doc = json.loads(out.read_text())
    doc["concepts"][0]["label"] = "Edited"
    out.write_text(ecso.serialize(doc))
    assert ecso.main(["check", "--out", str(out)]) == 1
    assert "1 edited" in capsys.readouterr().err


def test_check_fails_without_a_recorded_commit(tmp_path):
    out = tmp_path / "concepts.json"
    out.write_text(json.dumps({"concepts": []}))
    assert ecso.main(["check", "--out", str(out)]) == 1


def test_bundled_table_is_from_a_numbered_ecso_file():
    src = json.loads(ecso.DEFAULT_OUT.read_text())["source"]
    assert ecso.OWL_NAME.match(src["path"].removeprefix("ecso/"))
    assert src["url"].endswith(f"/{src['commit']}/{src['path']}")


def test_raw_download_errors_are_raised():
    with respx.mock:
        respx.get(f"{ecso.GITHUB_RAW}/DataONEorg/sem-prov-ontologies/{COMMIT}/ecso/ECSO8.owl").respond(404)
        with httpx.Client() as http, pytest.raises(httpx.HTTPStatusError):
            ecso.download(http, COMMIT, "ecso/ECSO8.owl")
