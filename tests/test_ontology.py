import httpx
import pytest

from arcticdata_mcp import ontology
from conftest import MN

SOIL_DEPTH = "http://purl.dataone.org/odo/ECSO_00001207"


def solr_json(docs, num=None, facet_queries=None):
    out = {"response": {"numFound": len(docs) if num is None else num, "start": 0, "docs": docs}}
    if facet_queries is not None:
        out["facet_counts"] = {"facet_queries": facet_queries}
    return out


@pytest.mark.parametrize("value", ["ECSO_00001207", "ecso:ECSO_00001207", SOIL_DEPTH, "https://purl.dataone.org/odo/ECSO_00001207"])
def test_concept_ids_normalize_and_cover_http_and_https(value):
    assert set(ontology.purl_variants(value)) == {SOIL_DEPTH, "https://purl.dataone.org/odo/ECSO_00001207"}
    assert ontology.concept_by_uri(value).label == "Soil Depth"


def test_other_uris_pass_through():
    assert ontology.purl_variants("http://purl.obolibrary.org/obo/ENVO_00002003") == ["http://purl.obolibrary.org/obo/ENVO_00002003"]
    assert ontology.annotation_clause(["*ECSO_00001207"]) == "sem_annotation:(*ECSO_00001207)"


def test_annotation_clause_ors_variants():
    clause = ontology.annotation_clause(["ECSO_00001207"])
    assert clause == f'sem_annotation:("{SOIL_DEPTH}" OR "https://purl.dataone.org/odo/ECSO_00001207")'


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
    assert {"Longwave_Albedo_MOV", "Shortwave_Albedo_MOV"} <= {c["label"] for c in sc["other_candidates"]}


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
    assert route.calls.last.request.url.params["q"] == f'(sem_annotation:("{SOIL_DEPTH}" OR "https://purl.dataone.org/odo/ECSO_00001207"))'


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
