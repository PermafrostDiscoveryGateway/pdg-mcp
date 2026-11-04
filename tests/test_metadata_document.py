import json

import httpx
import pytest
from fastmcp.exceptions import ToolError

from arcticdata_mcp import eml
from conftest import MN

PID = "doi:10.18739/TEST"
OBJECT = f"{MN}/object/doi%3A10.18739%2FTEST"
METHODS = "Radar spectra were averaged over 2 s. " * 60


def attribute(n, name, unit="<standardUnit>second</standardUnit>"):
    # Same content in every table, but a different XML id each time, as metadata editors write it.
    return f"""<attribute id="a{n}-{name}"><attributeName>{name}</attributeName>
      <attributeDefinition>{f"{name} definition. " * 40}</attributeDefinition>
      <measurementScale><ratio><unit>{unit}</unit><numericDomain><numberType>real</numberType></numericDomain></ratio></measurementScale>
      <missingValueCode><code>-999</code><codeExplanation>No data</codeExplanation></missingValueCode>
      <annotation><propertyURI label="contains measurements of type">http://ecoinformatics.org/oboe/oboe.1.2/oboe-core.owl#containsMeasurementsOfType</propertyURI>
        <valueURI label="time of measurement">http://purl.dataone.org/odo/ECSO_00002040</valueURI></annotation>
    </attribute>"""


def daily_table(n):
    return f"""<dataTable id="t{n}"><entityName>radar.2022010{n}.nc</entityName><entityDescription>Day {n}</entityDescription>
      <physical><objectName>radar.2022010{n}.nc</objectName><size>100</size>
        <dataFormat><externallyDefinedFormat><formatName>netCDF-4</formatName></externallyDefinedFormat></dataFormat></physical>
      <attributeList id="list{n}">{attribute(n, "time")}{attribute(n, "reflectivity", "<customUnit>dBZ</customUnit>")}</attributeList>
    </dataTable>"""


EML = f"""<?xml version="1.0"?>
<eml:eml xmlns:eml="https://eml.ecoinformatics.org/eml-2.2.0" packageId="{PID}" system="knb">
  <dataset>
    <title>Cloud radar at Summit</title>
    <creator><individualName><givenName>Ada</givenName><surName>Lovelace</surName></individualName>
      <organizationName>CIRES</organizationName><userId directory="https://orcid.org">https://orcid.org/0000-0000-0000-0001</userId></creator>
    <pubDate>2024</pubDate>
    <abstract><para>Radar moments from Summit Station.</para></abstract>
    <keywordSet><keyword>ICECAPS</keyword></keywordSet>
    <coverage>
      <geographicCoverage><geographicDescription>Summit, Greenland</geographicDescription>
        <boundingCoordinates><westBoundingCoordinate>-38.5</westBoundingCoordinate><eastBoundingCoordinate>-38.4</eastBoundingCoordinate>
          <northBoundingCoordinate>72.6</northBoundingCoordinate><southBoundingCoordinate>72.5</southBoundingCoordinate></boundingCoordinates></geographicCoverage>
      <temporalCoverage><rangeOfDates><beginDate><calendarDate>2022-01-01</calendarDate></beginDate>
        <endDate><calendarDate>2022-12-31</calendarDate></endDate></rangeOfDates></temporalCoverage>
    </coverage>
    <annotation><propertyURI label="theme">http://www.w3.org/ns/dcat#theme</propertyURI>
      <valueURI label="Atmospheric Science">https://purl.dataone.org/odo/ADCAD_00039</valueURI></annotation>
    <contact><organizationName>NOAA</organizationName><electronicMailAddress>x@noaa.gov</electronicMailAddress></contact>
    <methods><methodStep><description><para>{METHODS}</para></description></methodStep></methods>
    <project><title>ICECAPS</title>
      <personnel><individualName><surName>Neely</surName></individualName><role>coPrincipalInvestigator</role></personnel>
      <award><funderName>National Science Foundation</funderName><awardNumber>1303879</awardNumber><title>ICECAPS award</title></award>
    </project>
    {daily_table(1)}{daily_table(2)}{daily_table(3)}
    <dataTable id="t9"><entityName>sites.csv</entityName>
      <attributeList><attribute id="s1"><attributeName>site</attributeName><attributeDefinition>Site code</attributeDefinition>
        <measurementScale><nominal><nonNumericDomain><enumeratedDomain>
          <codeDefinition><code>SUM</code><definition>Summit</definition></codeDefinition></enumeratedDomain></nonNumericDomain></nominal></measurementScale>
      </attribute></attributeList></dataTable>
    <dataTable id="t10"><entityName>sites_copy.csv</entityName><attributeList><references>list1</references></attributeList></dataTable>
    <otherEntity><entityName>report.pdf</entityName><entityType>PDF</entityType></otherEntity>
  </dataset>
  <additionalMetadata><metadata><unitList>
    <unit id="dBZ" name="dBZ" unitType="dimensionless"><description>Decibels relative to Z</description></unit>
  </unitList></metadata></additionalMetadata>
</eml:eml>"""

ISO = '<?xml version="1.0"?><gmi:MI_Metadata xmlns:gmi="http://www.isotc211.org/2005/gmi"><title>ISO</title></gmi:MI_Metadata>'


def load(xml):
    return eml.parse(xml)[1]


@pytest.fixture
def doc():
    return load(EML)


def test_identical_schemas_are_deduplicated(doc):
    groups = doc.entity_groups()
    assert len(doc.schemas) == 2
    [daily] = [g for g in groups if g.get("schema_id") == "S1"]
    # Three daily tables plus one that points at the first one's list via <references>.
    assert daily["n_entities"] == 4 and daily["n_attributes"] == 2
    assert daily["attribute_names"] == ["time", "reflectivity"]
    assert daily["example_entities"] == ["radar.20220101.nc", "radar.20220102.nc", "radar.20220103.nc"]
    [other] = [g for g in groups if "schema_id" not in g]
    assert other == {"entity_types": {"otherEntity": 1}, "n_entities": 1, "n_attributes": 0, "example_entities": ["report.pdf"]}


def test_attributes_are_copied_whole(doc):
    time, refl = doc.schemas["S1"]
    assert time["attributeName"] == "time" and "id" not in time
    assert time["measurementScale"] == {"ratio": {"unit": {"standardUnit": "second"}, "numericDomain": {"numberType": "real"}}}
    assert time["missingValueCode"] == {"code": "-999", "codeExplanation": "No data"}
    assert time["annotation"]["valueURI"] == {"label": "time of measurement", "#text": "http://purl.dataone.org/odo/ECSO_00002040"}
    assert refl["measurementScale"]["ratio"]["unit"]["customUnit"] == {
        "#text": "dBZ", "unit_definition": {"name": "dBZ", "unit_type": "dimensionless", "description": "Decibels relative to Z"},
    }
    [site] = doc.schemas["S2"]
    assert eml.attribute_codes(site) == [{"code": "SUM", "definition": "Summit"}]


def column(scale):
    return f"<attribute><attributeName>x</attributeName><measurementScale>{scale}</measurementScale></attribute>"


def one_table(*attrs, extra=""):
    tables = "".join(f'<dataTable id="t{i}"><entityName>t{i}</entityName><attributeList>{a}</attributeList></dataTable>' for i, a in enumerate(attrs))
    return f'<eml:eml xmlns:eml="https://eml.ecoinformatics.org/eml-2.2.0"><dataset><title>T</title>{tables}{extra}</dataset></eml:eml>'


def test_tree_keeps_what_fixed_paths_dropped():
    d = load(one_table(
        column("""<dateTime><formatString>YYYY</formatString><dateTimeDomain>
          <bounds><minimum exclusive="false">1990</minimum></bounds><bounds><maximum exclusive="true">2030</maximum></bounds>
          </dateTimeDomain></dateTime>"""),
        column('<nominal><nonNumericDomain><references>codes</references></nonNumericDomain></nominal>'),
        column("""<nominal><nonNumericDomain><enumeratedDomain><entityCodeList><entityReference>t0</entityReference>
          <valueAttributeReference>a</valueAttributeReference><definitionAttributeReference>b</definitionAttributeReference>
          </entityCodeList></enumeratedDomain></nonNumericDomain></nominal>"""),
        extra="""<additionalMetadata><nonNumericDomain id="codes"><enumeratedDomain>
          <codeDefinition><code>A</code><definition>Alpha</definition></codeDefinition>
          <codeDefinition><code>B</code><definition>Beta</definition></codeDefinition></enumeratedDomain></nonNumericDomain></additionalMetadata>""",
    ))
    [date], [ref], [ecl] = d.schemas.values()
    assert date["measurementScale"]["dateTime"]["dateTimeDomain"]["bounds"] == [
        {"minimum": {"exclusive": "false", "#text": "1990"}}, {"maximum": {"exclusive": "true", "#text": "2030"}},
    ]
    assert [c["code"] for c in eml.attribute_codes(ref)] == ["A", "B"]
    assert ecl["measurementScale"]["nominal"]["nonNumericDomain"]["enumeratedDomain"]["entityCodeList"]["entityReference"] == "t0"


def test_columns_differing_only_in_a_flag_get_different_schemas():
    bound = '<ratio><unit><standardUnit>meter</standardUnit></unit><numericDomain><numberType>real</numberType><bounds><minimum exclusive="{}">0</minimum></bounds></numericDomain></ratio>'
    d = load(one_table(column(bound.format("true")), column(bound.format("false"))))
    assert len(d.schemas) == 2


def test_unresolved_reference_is_flagged():
    d = load(one_table("<references>nowhere</references>"))
    assert d.entities[0]["unresolved_reference"] == "nowhere" and "schema_id" not in d.entities[0]
    assert d.outline()["unresolved_references"] == ["nowhere"]


def test_temporal_coverage_ignores_empty_and_non_iso_dates():
    cov = """<coverage><temporalCoverage><singleDateTime> </singleDateTime></temporalCoverage>
      <temporalCoverage><singleDateTime><calendarDate>2010</calendarDate></singleDateTime></temporalCoverage>
      <temporalCoverage><rangeOfDates><beginDate><calendarDate>2009-06-01</calendarDate><time>12:00:00</time></beginDate>
        <endDate><calendarDate>2011-01-01</calendarDate></endDate></rangeOfDates></temporalCoverage>
      <temporalCoverage><singleDateTime><alternativeTimeScale><timeScaleName>Ma</timeScaleName><timeScaleAgeEstimate>66</timeScaleAgeEstimate>
        </alternativeTimeScale></singleDateTime></temporalCoverage></coverage>"""
    d = load(one_table(extra=cov))
    assert d.outline()["coverage"]["temporal"] == {"begin": "2009-06-01T12:00:00", "end": "2011-01-01", "other_values": ["Ma 66"]}
    empty = load(one_table(extra="<coverage><temporalCoverage><singleDateTime/></temporalCoverage></coverage>"))
    assert "temporal" not in empty.outline().get("coverage", {})


def test_fit_codes_cuts_an_oversized_code_list():
    codes = "".join(f"<codeDefinition><code>C{i}</code><definition>Station {i}</definition></codeDefinition>" for i in range(2000))
    [[attr]] = load(one_table(column(f"<nominal><nonNumericDomain><enumeratedDomain>{codes}</enumeratedDomain></nonNumericDomain></nominal>"))).schemas.values()
    fitted = eml.fit_codes(attr, 1000)
    assert len(json.dumps(fitted)) <= 1000
    kept = eml.attribute_codes(fitted)
    assert kept and kept == eml.attribute_codes(attr)[:len(kept)]
    assert fitted["codeDefinition_omitted"] == 2000 - len(kept)
    assert eml.fit_codes(attr, 10**7) is attr


def test_outline(doc):
    o = doc.outline()
    assert o["title"] == "Cloud radar at Summit" and o["creators"] == ["Ada Lovelace"]
    assert o["abstract"] == "Radar moments from Summit Station."
    assert o["coverage"]["bounding_box"] == {"west": -38.5, "east": -38.4, "north": 72.6, "south": 72.5}
    assert o["coverage"]["temporal"] == {"begin": "2022-01-01", "end": "2022-12-31"}
    assert o["methods_chars"] == len(METHODS.strip()) and o["methods_preview"].endswith(" ...")
    assert o["annotations"][0]["value"] == "Atmospheric Science"
    assert o["n_entities"] == 6 and o["n_schemas"] == 2
    assert len(json.dumps(o)) < len(EML) / 2


def test_find_schema(doc):
    assert doc.find_schema(None, "sites.csv") == "S2"
    assert doc.find_schema(None, "sites_copy.csv") == "S1"
    with pytest.raises(ToolError, match="pass schema_id"):
        doc.find_schema(None, None)
    with pytest.raises(ToolError, match="no attribute list"):
        doc.find_schema(None, "report.pdf")
    with pytest.raises(ToolError, match="Unknown schema_id"):
        doc.find_schema("S7", None)


def test_sections(doc):
    people = doc.people()
    assert people["creators"][0]["user_id"] == ["https://orcid.org/0000-0000-0000-0001"]
    assert people["contacts"] == [{"organization": "NOAA", "email": ["x@noaa.gov"]}]
    [project] = doc.project()
    assert project["awards"] == [{"funder": "National Science Foundation", "award_number": "1303879", "title": "ICECAPS award"}]
    assert project["personnel"] == [{"name": "Neely", "role": "coPrincipalInvestigator"}]
    anns = doc.annotations()
    assert [a["attribute"] for a in anns["attributes"]["S1"]] == ["time", "reflectivity"]
    assert anns["dataset"][0]["property"] == "theme"


def test_antimeridian_boxes_are_not_merged():
    two = EML.replace("</coverage>", """<geographicCoverage><geographicDescription>Chukchi</geographicDescription>
      <boundingCoordinates><westBoundingCoordinate>170</westBoundingCoordinate><eastBoundingCoordinate>-170</eastBoundingCoordinate>
      <northBoundingCoordinate>75</northBoundingCoordinate><southBoundingCoordinate>70</southBoundingCoordinate></boundingCoordinates>
      </geographicCoverage></coverage>""")
    cov = load(two).coverage_summary()
    assert cov["n_geographic"] == 2 and "bounding_box" not in cov


def test_missing_sections():
    bare = load('<eml:eml xmlns:eml="https://eml.ecoinformatics.org/eml-2.2.0"><dataset><title>T</title></dataset></eml:eml>')
    assert bare.outline() == {"format": "EML", "eml_namespace": "https://eml.ecoinformatics.org/eml-2.2.0", "title": "T", "n_entities": 0, "n_schemas": 0}
    assert bare.methods_text() is None and bare.coverage() == {} and bare.project() == []


def test_page_items():
    items = [{"n": i} for i in range(10)]
    page, nxt = eml.page_items(items, 0, 30)
    assert page == items[:3] and nxt == 3
    assert eml.page_items(items, 9, 30) == ([{"n": 9}], None)
    # One item is always returned, even if it is larger than the budget.
    assert eml.page_items([{"x": "y" * 100}], 0, 10)[0] == [{"x": "y" * 100}]
    assert eml.page_items([], 0, 10) == ([], None)
    with pytest.raises(ToolError, match="offset 10 is past the end"):
        eml.page_items(items, 10, 30)


def test_page_text():
    assert eml.page_text("abcdef", 2, 3) == ("cde", 5)
    assert eml.page_text("abcdef", 5, 3) == ("f", None)
    with pytest.raises(ToolError, match=r"offset 50000 is past the end \(6 chars\)"):
        eml.page_text("abcdef", 50_000, 3)


async def test_tool_defaults_to_outline(client, mock_api):
    mock_api.get(OBJECT).mock(return_value=httpx.Response(200, text=EML))
    r = (await client.call_tool("get_metadata_document", {"pid": PID})).structured_content
    assert r["section"] == "outline" and r["document_chars"] == len(EML)
    assert r["entity_groups"][0]["schema_id"] == "S1" and "document" not in r


async def test_tool_attributes_paging(client, mock_api):
    mock_api.get(OBJECT).mock(return_value=httpx.Response(200, text=EML))
    call = {"pid": PID, "section": "attributes", "entity": "radar.20220102.nc", "max_chars": 1000}
    first = (await client.call_tool("get_metadata_document", call)).structured_content
    assert first["schema_id"] == "S1" and first["n_entities"] == 4 and first["n_attributes"] == 2
    assert [a["attributeName"] for a in first["attributes"]] == ["time"] and first["next_offset"] == 1
    rest = (await client.call_tool("get_metadata_document", {**call, "offset": 1})).structured_content
    assert [a["attributeName"] for a in rest["attributes"]] == ["reflectivity"] and rest["next_offset"] is None


async def test_tool_entities_and_methods(client, mock_api):
    mock_api.get(OBJECT).mock(return_value=httpx.Response(200, text=EML))
    ents = (await client.call_tool("get_metadata_document", {"pid": PID, "section": "entities"})).structured_content
    assert ents["n_entities"] == 6 and ents["next_offset"] is None
    assert ents["entities"][0] == {
        "index": 0, "type": "dataTable", "name": "radar.20220101.nc", "id": "t1", "description": "Day 1",
        "format": "netCDF-4", "size": "100", "schema_id": "S1", "n_attributes": 2,
    }
    first = (await client.call_tool("get_metadata_document", {"pid": PID, "section": "methods", "max_chars": 1000})).structured_content
    rest = (await client.call_tool("get_metadata_document", {"pid": PID, "section": "methods", "offset": 1000})).structured_content
    assert first["next_offset"] == 1000 and rest["next_offset"] is None
    assert first["text"] + rest["text"] == METHODS.strip()


async def test_tool_raw_paging(client, mock_api):
    mock_api.get(OBJECT).mock(return_value=httpx.Response(200, text=EML))
    r = (await client.call_tool("get_metadata_document", {"pid": PID, "section": "raw", "offset": 10, "max_chars": 1000})).structured_content
    assert r["document"] == EML[10:1010] and r["next_offset"] == 1010


async def test_tool_non_eml_falls_back_to_raw(client, mock_api):
    mock_api.get(OBJECT).mock(return_value=httpx.Response(200, text=ISO))
    r = (await client.call_tool("get_metadata_document", {"pid": PID})).structured_content
    assert r["format"] == "MI_Metadata" and r["document"] == ISO and r["next_offset"] is None
    with pytest.raises(ToolError, match="only available for EML"):
        await client.call_tool("get_metadata_document", {"pid": PID, "section": "coverage"})


async def test_tool_eml_without_dataset_falls_back_to_raw(client, mock_api):
    software = '<eml:eml xmlns:eml="https://eml.ecoinformatics.org/eml-2.2.0"><software><title>Tool</title></software></eml:eml>'
    mock_api.get(OBJECT).mock(return_value=httpx.Response(200, text=software))
    r = (await client.call_tool("get_metadata_document", {"pid": PID})).structured_content
    assert r["format"] == "eml/software" and r["document"] == software
    with pytest.raises(ToolError, match=r"<eml/software>"):
        await client.call_tool("get_metadata_document", {"pid": PID, "section": "people"})


async def test_tool_pages_oversized_codes(client, mock_api):
    codes = "".join(f"<codeDefinition><code>C{i}</code><definition>Station {i}</definition></codeDefinition>" for i in range(500))
    xml = one_table(column(f"<nominal><nonNumericDomain><enumeratedDomain>{codes}</enumeratedDomain></nonNumericDomain></nominal>"))
    mock_api.get(OBJECT).mock(return_value=httpx.Response(200, text=xml))
    r = (await client.call_tool("get_metadata_document", {"pid": PID, "section": "attributes", "max_chars": 2000})).structured_content
    assert len(json.dumps(r["attributes"])) <= 2000 and r["attributes"][0]["codeDefinition_omitted"] > 0 and "note" in r
    seen, offset = [], 0
    while offset is not None:
        page = (await client.call_tool("get_metadata_document", {"pid": PID, "section": "codes", "attribute": "x", "offset": offset, "max_chars": 2000})).structured_content
        assert len(json.dumps(page["codes"])) <= 2000
        seen += page["codes"]
        offset = page["next_offset"]
    assert [c["code"] for c in seen] == [f"C{i}" for i in range(500)] and page["n_codes"] == 500


async def test_tool_fetches_each_document_once_while_paging(client, mock_api):
    route = mock_api.get(OBJECT).mock(return_value=httpx.Response(200, text=EML))
    for offset in (0, 1000):
        await client.call_tool("get_metadata_document", {"pid": PID, "section": "raw", "offset": offset, "max_chars": 1000})
    await client.call_tool("get_metadata_document", {"pid": PID, "section": "entities"})
    assert route.call_count == 1


def test_document_cache_evicts_oldest_and_expired():
    import asyncio

    async def run():
        cache, fetched = eml.DocumentCache(max_entries=2, ttl=60), []

        def fetcher(pid):
            async def fetch():
                fetched.append(pid)
                return pid
            return fetch
        for pid in ("a", "b", "a", "c", "b"):
            await cache.get(pid, fetcher(pid))
        assert fetched == ["a", "b", "c", "b"]  # "b" was evicted when "c" arrived
        cache.ttl = 0
        await cache.get("b", fetcher("b"))
        assert fetched[-1] == "b" and len(fetched) == 5
    asyncio.run(run())


FIELD_LIST = """<queryEngineDescription><name>solr</name>
  <queryField><name>id</name><returnable>true</returnable></queryField>
  <queryField><name>title</name><returnable>true</returnable></queryField>
  <queryField><name>licenseName</name><returnable>true</returnable></queryField>
  <queryField><name>attributeName</name><returnable>true</returnable></queryField>
  <queryField><name>text</name><returnable>true</returnable></queryField>
  <queryField><name>documents</name><returnable>true</returnable></queryField>
  <queryField><name>_version_</name><returnable>true</returnable></queryField>
  <queryField><name>hidden</name><returnable>false</returnable></queryField>
</queryEngineDescription>"""
FULL_DOC = {"id": PID, "title": "T", "attribute": ["a"], "attributeName": ["a"], "text": "t", "documents": ["d"], "_version_": 1}


async def test_dataset_details_uses_index_field_list(client, mock_api):
    fields = mock_api.get(f"{MN}/query/solr").mock(return_value=httpx.Response(200, text=FIELD_LIST))
    route = mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(200, json={"response": {"numFound": 1, "docs": [{"id": PID}]}}))
    await client.call_tool("get_dataset_details", {"pid": PID})
    await client.call_tool("get_dataset_details", {"pid": PID})
    assert route.calls.last.request.url.params["fl"] == "id,title,licenseName"
    assert fields.call_count == 1  # cached
    await client.call_tool("get_dataset_details", {"pid": PID, "fields": "*"})
    assert route.calls.last.request.url.params["fl"] == "*"


async def test_dataset_details_falls_back_when_field_list_fails(client, mock_api):
    mock_api.get(f"{MN}/query/solr").mock(return_value=httpx.Response(500, text="boom"))
    route = mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(200, json={"response": {"numFound": 1, "docs": [FULL_DOC]}}))
    r = (await client.call_tool("get_dataset_details", {"pid": PID})).structured_content
    assert route.calls.last.request.url.params["fl"] == "*"
    assert {k for k in r if k != "landing_url"} == {"id", "title"}
