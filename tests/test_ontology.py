import httpx
import pytest

from arcticdata_mcp import ontology
from conftest import MN

SOIL_DEPTH = "http://purl.dataone.org/odo/ECSO_00001207"
SOIL_DEPTH_FORMS = [SOIL_DEPTH, "https://purl.dataone.org/odo/ECSO_00001207", "http://www.purl.dataone.org/odo/ECSO_00001207"]
WATER_CONTENT = "http://purl.dataone.org/odo/ECSO_00010077"  # minted as http://www.purl..., 96 current datasets


def solr_json(docs, num=None, facet_queries=None):
    out = {"response": {"numFound": len(docs) if num is None else num, "start": 0, "docs": docs}}
    if facet_queries is not None:
        out["facet_counts"] = {"facet_queries": facet_queries}
    return out


@pytest.mark.parametrize("value", ["ECSO_00001207", "ecso:ECSO_00001207", *SOIL_DEPTH_FORMS])
def test_concept_ids_normalize_and_cover_every_purl_form(value):
    assert ontology.normalize_concept_id(value) == SOIL_DEPTH
    assert ontology.purl_variants(value) == SOIL_DEPTH_FORMS
    assert ontology.concept_by_uri(value).label == "Soil Depth"


@pytest.mark.parametrize("value", ["ECSO_00010077", WATER_CONTENT, "http://www.purl.dataone.org/odo/ECSO_00010077"])
def test_www_purl_concepts_are_bundled(value):
    assert ontology.concept_by_uri(value).label == "Water Content"
    assert "http://www.purl.dataone.org/odo/ECSO_00010077" in ontology.purl_variants(value)


@pytest.mark.parametrize(("term", "label"), [("water content", "Water Content"), ("depth of thaw", "Depth of Thaw"), ("NDVI", "Normalized Difference Vegetation Index")])
def test_www_purl_concepts_resolve(term, label):
    assert [m.concept.label for m in ontology.top_matches(term, ontology.match_local(term, 50))] == [label]


def test_bundled_table_records_its_ecso_version():
    import json
    from importlib.resources import files

    source = json.loads(files("arcticdata_mcp").joinpath("data/ecso/concepts.json").read_text())["source"]
    assert source["repository"] == "DataONEorg/sem-prov-ontologies" and len(source["commit"]) == 40
    assert source["exported"]


def test_other_uris_pass_through():
    assert ontology.purl_variants("http://purl.obolibrary.org/obo/ENVO_00002003") == ["http://purl.obolibrary.org/obo/ENVO_00002003"]
    assert ontology.annotation_clause(["*ECSO_00001207"]) == "sem_annotation:(*ECSO_00001207)"


def test_annotation_clause_ors_variants():
    clause = ontology.annotation_clause(["ECSO_00001207"])
    assert clause == "sem_annotation:(" + " OR ".join(f'"{u}"' for u in SOIL_DEPTH_FORMS) + ")"


@pytest.mark.parametrize(
    ("term", "label"),
    [("soil depth", "Soil Depth"), ("Soil depths", "Soil Depth"), ("active layer thickness", "Soil Active Layer Depth"), ("methane flux", "Methane Flux")],
)
def test_match_local_ranks_exact_label_or_synonym_first(term, label):
    best = ontology.match_local(term)[0]
    assert best.concept.label == label
    assert best.score == 1.0


def test_match_local_weak_match_stays_below_auto_threshold():
    matches = ontology.match_local("permafrost temperature")
    assert matches and matches[0].score < ontology.STRONG_MATCH


@pytest.mark.parametrize(
    ("term", "labels"),
    [
        ("albedo", ["albedo"]),  # not Longwave_/Shortwave_Albedo_MOV, which only list it as a synonym
        ("flux", ["Flux"]),  # not Radiative Flux
        ("active layer thickness", ["Soil Active Layer Depth"]),  # synonym-only match is still used
    ],
)
def test_top_matches_prefers_concepts_whose_own_label_matches(term, labels):
    assert [m.concept.label for m in ontology.top_matches(term, ontology.match_local(term, 10))] == labels


@pytest.mark.parametrize(("term", "label"), [("moisture", "Volumetric Water Content Method"), ("animal", "Multi-Celled Organism")])
def test_related_synonym_is_a_candidate_but_never_used_automatically(term, label):
    matches = ontology.match_local(term, 30)
    m = next(m for m in matches if m.concept.label == label)
    assert m.score < ontology.STRONG_MATCH
    assert m.to_dict()["matched_as"] == "related_synonym"
    assert label not in [t.concept.label for t in ontology.top_matches(term, matches)]


def test_to_dict_keeps_exact_and_related_synonyms_apart():
    c = next(c for c in ontology.load_concepts() if c.label == "dissolved organic carbon")
    d = ontology.Match(c, 1.0, "DOC").to_dict()
    assert d["matched_as"] == "synonym"
    assert "DOC" in d["synonyms"] and "dissolved organic material" not in d["synonyms"]
    assert d["related_synonyms"] == ["dissolved organic material"]


def test_top_matches_empty_when_best_match_is_weak():
    assert ontology.top_matches("permafrost temperature", ontology.match_local("permafrost temperature", 10)) == []


def test_top_matches_keeps_all_label_ties():
    labels = {m.concept.label.lower() for m in ontology.top_matches("depth", ontology.match_local("depth", 10))}
    assert labels == {"depth"}
    assert len(ontology.top_matches("depth", ontology.match_local("depth", 10))) == 2


async def test_search_by_variable_uses_only_label_match_for_ties(client, mock_api):
    mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(200, json=solr_json([], facet_queries={})))
    r = await client.call_tool("search_by_variable", {"variable": "albedo", "rows": 0})
    sc = r.structured_content
    assert sc["resolution"] == "concept"
    assert [c["label"] for c in sc["concepts_used"]] == ["albedo"]
    # Longwave_/Shortwave_Albedo_MOV sit under albedo, so searching albedo already finds them.
    assert not {"Longwave_Albedo_MOV", "Shortwave_Albedo_MOV"} & {c["label"] for c in sc.get("other_candidates", [])}
    assert "ambiguous" not in sc


def test_without_descendants_keeps_the_ancestor():
    tied = ontology.top_matches("temperature", ontology.match_local("temperature", 50))
    kept = ontology.without_descendants(tied)
    assert "temperature_measurementType" in {m.concept.label for m in kept}
    assert "Soil Temperature" not in {m.concept.label for m in kept}
    # Every temperature concept sits under temperature_measurementType, so it alone covers them all.
    assert [m.concept.label for m in kept] == ["temperature_measurementType"]
    assert all(not ontology.ancestors(m.concept.uri) & {k.concept.uri for k in kept} for m in kept)


async def test_search_by_variable_breaks_wide_ties_by_dataset_count(client, mock_api):
    kept = ontology.without_descendants(ontology.top_matches("soil", ontology.match_local("soil", 50)))
    assert len(kept) > 5
    # The last tied concepts have the most datasets, so they are the ones searched.
    counts = {f"c{i}": i for i in range(len(kept))}
    mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(200, json=solr_json([], facet_queries=counts)))
    r = await client.call_tool("search_by_variable", {"variable": "soil", "rows": 0})
    sc = r.structured_content
    assert sc["resolution"] == "concept" and sc["ambiguous"] is True
    assert [c["uri"] for c in sc["concepts_used"]] == [m.concept.uri for m in reversed(kept[-5:])]
    dropped = [c for c in sc["other_candidates"] if "dataset_count" in c]
    assert [c["uri"] for c in dropped] == [m.concept.uri for m in reversed(kept[:-5])]
    assert "equally well" in sc["note"]


def test_merge_combines_sources_and_keeps_best_score():
    c = ontology.concept_by_uri(SOIL_DEPTH)
    merged = ontology.merge([ontology.Match(c, 0.7, "x")], [ontology.Match(c, 0.9, "y", ["bioportal"])])
    assert len(merged) == 1
    assert merged[0].score == 0.9 and merged[0].sources == ["ecso_table", "bioportal"]


async def test_find_variable_concepts_adds_counts(client, mock_api):
    route = mock_api.get(f"{MN}/query/solr/").mock(
        return_value=httpx.Response(200, json=solr_json([], facet_queries={"c0": 122, "c1": 74, "c2": 0}))
    )
    r = await client.call_tool("find_variable_concepts", {"term": "soil depth", "limit": 3})
    sc = r.structured_content
    assert sc["latest_only"] is True and sc["bioportal"] == "not used"
    assert sc["concepts"][0]["uri"] == SOIL_DEPTH and sc["concepts"][0]["dataset_count"] == 122
    params = route.calls.last.request.url.params
    assert "-obsoletedBy:*" in params.get_list("fq")
    assert any(SOIL_DEPTH in fq for fq in params.get_list("facet.query"))


async def test_search_by_variable_labels_match_type(client, mock_api):
    docs = [
        {"id": "doi:a", "title": "A", "sem_annotation": [SOIL_DEPTH]},
        {"id": "doi:b", "title": "B", "sem_annotation": ["https://purl.dataone.org/odo/ECSO_00001207"]},
        {"id": "doi:c", "title": "C"},
    ]
    route = mock_api.get(f"{MN}/query/solr/").mock(side_effect=[
        httpx.Response(200, json=solr_json(docs, num=139, facet_queries={"annotation": 122, "free_text": 25})),
        httpx.Response(200, json=solr_json([{"id": "doi:a"}, {"id": "doi:c"}])),
    ])
    r = await client.call_tool("search_by_variable", {"variable": "soil depth", "rows": 3})
    sc = r.structured_content
    assert sc["resolution"] == "concept"
    assert sc["concepts_used"] == [{"uri": SOIL_DEPTH, "label": "Soil Depth"}]
    assert sc["numFound"] == 139 and sc["counts"] == {"annotation": 122, "free_text": 25}
    assert [x["match_type"] for x in sc["results"]] == ["both", "annotation", "free_text"]
    assert all("sem_annotation" not in x for x in sc["results"])
    q = route.calls[0].request.url.params["q"]
    assert "sem_annotation:" in q and 'attributeName:"soil depth"' in q


async def test_search_by_variable_falls_back_to_free_text(client, mock_api):
    route = mock_api.get(f"{MN}/query/solr/").mock(
        return_value=httpx.Response(200, json=solr_json([{"id": "doi:x", "title": "X"}], facet_queries={"free_text": 1}))
    )
    r = await client.call_tool("search_by_variable", {"variable": "permafrost temperature"})
    sc = r.structured_content
    assert sc["resolution"] == "free_text_fallback" and sc["concepts_used"] == []
    assert sc["results"][0]["match_type"] == "free_text"
    assert sc["other_candidates"]
    assert route.call_count == 1
    assert route.calls.last.request.url.params["q"] == '(attribute:("permafrost" AND "temperature"))'


async def test_search_by_variable_explicit_concepts(client, mock_api):
    route = mock_api.get(f"{MN}/query/solr/").mock(
        return_value=httpx.Response(200, json=solr_json([], facet_queries={"annotation": 0}))
    )
    r = await client.call_tool(
        "search_by_variable", {"variable": "depth", "concept_uris": ["ECSO_00001207"], "include_free_text": False},
    )
    assert r.structured_content["resolution"] == "explicit"
    assert route.calls.last.request.url.params["q"] == "(" + ontology.annotation_clause([SOIL_DEPTH]) + ")"


async def test_bioportal_used_when_configured(settings, mock_api):
    from fastmcp import Client

    from arcticdata_mcp.server import create_server

    settings.ontology.bioportal_api_key = "k"
    bp = mock_api.get("https://data.bioontology.org/search").mock(return_value=httpx.Response(200, json={"collection": [
        {"@id": "http://purl.dataone.org/odo/ECSO_00001697", "prefLabel": "permafrost depth"},
    ]}))
    async with Client(create_server(settings, require_auth=False)) as c:
        r = await c.call_tool("find_variable_concepts", {"term": "permafrost", "with_counts": False})
    assert bp.calls.last.request.headers["Authorization"] == "apikey token=k"
    sc = r.structured_content
    assert sc["bioportal"] == "used"
    hit = next(x for x in sc["concepts"] if x["uri"].endswith("ECSO_00001697"))
    assert "bioportal" in hit["source"]


async def test_bioportal_failure_degrades_to_local_table(settings, mock_api):
    from fastmcp import Client

    from arcticdata_mcp.server import create_server

    settings.ontology.bioportal_api_key = "bad"
    mock_api.get("https://data.bioontology.org/search").mock(return_value=httpx.Response(401, text="unauthorized"))
    async with Client(create_server(settings, require_auth=False)) as c:
        r = await c.call_tool("find_variable_concepts", {"term": "soil depth", "with_counts": False})
    sc = r.structured_content
    assert sc["concepts"][0]["label"] == "Soil Depth"
    assert "BioPortal lookup failed" in sc["warning"] and sc["bioportal"] == "not used"


async def test_semantic_annotation_tool_accepts_bare_id(client, mock_api):
    route = mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(200, json=solr_json([], num=122)))
    r = await client.call_tool("search_by_semantic_annotation", {"concept_uri": "ECSO_00001207", "rows": 0})
    assert r.structured_content["concept"]["label"] == "Soil Depth"
    assert r.structured_content["latest_only"] is True
    assert "https://purl.dataone.org/odo/ECSO_00001207" in route.calls.last.request.url.params["q"]
