"""Schema completeness: every element and attribute of each supported EML version reaches some section of
get_metadata_document, or is listed in eml.IGNORED or eml.KNOWN_GAPS (issue #29).

The schemas are bundled in tests/data/eml-schema. Supporting a new EML version means adding its schema files
there and its namespace to eml.SUPPORTED_NAMESPACES; this test then lists whatever the parser misses.
"""

import pytest
from eml_coverage import ROOT_NAMESPACES, explained, matches, missing_paths, schema_document, value_paths

from arcticdata_mcp import eml

VERSIONS = sorted(ROOT_NAMESPACES)
# Values that reach no section only because of how the schema document is built, not because the parser misses
# them in real documents. Exact paths ("*" allowed), so nothing below them is excused.
ARTIFACTS = {
    "dataset/*/attributeList": "each type is filled in once (for dataTable); other entities get an attributeList "
    "with text and no <attribute>, which no real document has",
    "dataset/coverage/taxonomicCoverage/taxonomicClassification": "the nested classification is text only, since "
    "its type is already filled in above it",
    "dataset/coverage/temporalCoverage/singleDateTime/alternativeTimeScale/*": "the document holds both sides of "
    "the calendarDate / alternativeTimeScale choice; a real date has one, and either is read",
}


def _artifact(path: str) -> bool:
    return any(matches(p, path, subtree=False) for p in ARTIFACTS)


@pytest.fixture(scope="module")
def missing() -> dict[str, dict[str, list[str]]]:
    return {v: missing_paths(schema_document(v)) for v in VERSIONS}


def test_supported_versions_have_schemas():
    assert VERSIONS == ["2.1.1", "2.2.0"]


@pytest.mark.parametrize("version", VERSIONS)
def test_schema_document_covers_the_schema(version):
    """Sanity check on the document builder: it reaches deep into the schema, not just the top level."""
    paths = value_paths(schema_document(version))
    assert len(paths) > 500
    for path in (
        "dataset/title",
        "dataset/abstract/section/para",
        "dataset/methods/methodStep/description",
        "dataset/dataTable/attributeList/attribute/attributeName",
        "dataset/dataTable/physical/authentication/@method",
        "dataset/project/personnel/role",
        "dataset/keywordSet/keywordThesaurus",
    ):
        assert path in paths, path
    if version == "2.2.0":
        assert "dataset/project/award/funderName" in paths and "dataset/licensed/licenseName" in paths


@pytest.mark.parametrize("version", VERSIONS)
def test_every_schema_value_reaches_a_section(version, missing):
    unexplained = [p for p in missing[version] if not explained(p) and not _artifact(p)]
    assert not unexplained, (
        f"EML {version} elements or attributes that no section returns. Handle them in eml.py, or add them to "
        "eml.IGNORED (with the reason) or eml.KNOWN_GAPS:\n" + "\n".join(unexplained)
    )


def test_lists_have_no_stale_entries(missing):
    """A fixed gap doesn't stay on KNOWN_GAPS, and every IGNORED entry names something the schemas define.

    IGNORED entries only need to exist: some (such as a followed <references>) are only dropped in real
    documents, where tests/test_live_eml.py meets them.
    """
    all_missing = {p for paths in missing.values() for p in paths}
    all_paths = {p for v in VERSIONS for p in value_paths(schema_document(v))}
    fixed = [p for p in eml.KNOWN_GAPS if not any(matches(p, m) for m in all_missing)]
    assert not fixed, f"now handled, remove from eml.KNOWN_GAPS: {fixed}"
    stale = [p for p in ARTIFACTS if not any(matches(p, m, subtree=False) for m in all_missing)]
    assert not stale, f"no longer needed, remove from ARTIFACTS: {stale}"
    unknown = [p for p in eml.IGNORED if not any(matches(p, m) for m in all_paths)]
    assert not unknown, f"not in any supported schema, check the path in eml.IGNORED: {unknown}"


def test_removing_handling_names_the_element(monkeypatch):
    monkeypatch.setattr(eml, "PEOPLE", {k: v for k, v in eml.PEOPLE.items() if k != "publisher"})
    monkeypatch.setattr(eml, "FREE_TEXT_SECTIONS", {k: v for k, v in eml.FREE_TEXT_SECTIONS.items() if k != "purpose"})
    found = missing_paths(schema_document("2.2.0"))
    assert "dataset/publisher" in found and not explained("dataset/publisher")
    assert "dataset/purpose" in found and not explained("dataset/purpose")


def test_matches():
    assert matches("access", "access/allow/principal")
    assert matches("**/@id", "dataset/creator/@id") and matches("**/@id", "@id")
    assert matches("dataset/coverage/*/references", "dataset/coverage/temporalCoverage/references")
    assert not matches("dataset/coverage/*/references", "dataset/coverage/references")
    assert not matches("dataset/title", "dataset/titles")
    assert not matches("dataset/*/attributeList", "dataset/otherEntity/attributeList/attribute", subtree=False)


def _outline(namespace: str) -> dict:
    return eml.parse(f'<eml:eml xmlns:eml="{namespace}"><dataset><title>T</title></dataset></eml:eml>')[1].outline()


@pytest.mark.parametrize("namespace", list(eml.SUPPORTED_NAMESPACES))
def test_supported_namespace_has_no_warning(namespace):
    assert "warning" not in _outline(namespace)


@pytest.mark.parametrize(
    "namespace", ["eml://ecoinformatics.org/eml-2.1.0", "https://eml.ecoinformatics.org/eml-2.3.0"]
)
def test_unsupported_namespace_warns(namespace):
    out = _outline(namespace)
    assert out["eml_namespace"] == namespace and out["title"] == "T"
    assert namespace in out["warning"] and "section='raw'" in out["warning"]
