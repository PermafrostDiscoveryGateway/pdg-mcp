import httpx
import pytest
from fastmcp.exceptions import ToolError

from arcticdata_mcp import eml
from conftest import MN

PID = "doi:10.18739/TEST"
OBJECT = f"{MN}/object/doi%3A10.18739%2FTEST"
LONG_STEP = "Cores were drilled to 3 m depth and logged at 10 cm intervals. " * 100

EML = f"""<?xml version="1.0"?>
<eml:eml xmlns:eml="https://eml.ecoinformatics.org/eml-2.2.0" packageId="{PID}" system="knb">
  <dataset>
    <title>Test</title>
    <abstract><para>Short abstract about permafrost cores.</para></abstract>
    <intellectualRights><para>CC-BY 4.0</para></intellectualRights>
    <methods>
      <methodStep>
        <description>
          <section><title>Field work</title><para>{LONG_STEP}</para></section>
          <itemizedlist><listitem><para>Item one</para></listitem></itemizedlist>
        </description>
        <dataSource><title>Cited dataset that is not narrative</title></dataSource>
      </methodStep>
      <sampling><studyExtent><description><para>Toolik</para></description></studyExtent>
        <samplingDescription><para>Random plots</para></samplingDescription></sampling>
    </methods>
    <dataTable><entityName>t.csv</entityName></dataTable>
  </dataset>
</eml:eml>"""


def test_free_text_section_flattens_methods():
    methods = eml.parse(EML)[1].free_text("methods")
    text = methods.text
    assert text.startswith("# Field work")
    assert "Cores were drilled" in text and "- Item one" in text
    assert "# Sampling" in text and "Random plots" in text
    assert "Cited dataset" not in text and "[dataSource 1]" in text
    assert methods.elements["data_sources"] == [{"ref": "dataSource 1", "title": "Cited dataset that is not narrative"}]


def test_free_text_section_missing():
    doc = eml.parse(EML)[1]
    assert doc.free_text("purpose") is None
    assert doc.free_text("abstract").text == "Short abstract about permafrost cores."


async def test_non_eml_documents_are_rejected(client, mock_api):
    mock_api.get(OBJECT).mock(return_value=httpx.Response(200, text="<MD_Metadata/>"))
    with pytest.raises(ToolError, match="only available for EML"):
        await client.call_tool("get_metadata_document", {"pid": PID, "section": "abstract"})


async def test_section_paging(client, mock_api):
    mock_api.get(OBJECT).mock(return_value=httpx.Response(200, text=EML))
    first = (await client.call_tool("get_metadata_document", {"pid": PID, "section": "methods", "max_chars": 1000})).structured_content
    assert first["next_offset"] == 1000 and len(first["text"]) == 1000
    rest = (await client.call_tool("get_metadata_document", {"pid": PID, "section": "methods", "offset": 1000, "max_chars": 100_000})).structured_content
    assert first["text"] + rest["text"] == eml.parse(EML)[1].free_text("methods").text
    assert rest["next_offset"] is None
    # Copied elements come back with the page their placeholder is on.
    assert "data_sources" not in first and rest["data_sources"][0]["ref"] == "dataSource 1"


async def test_raw_document_paging(client, mock_api):
    mock_api.get(OBJECT).mock(return_value=httpx.Response(200, text=EML))
    r = (await client.call_tool("get_metadata_document", {"pid": PID, "section": "raw", "offset": 10, "max_chars": 1000})).structured_content
    assert r["document"] == EML[10:1010] and r["next_offset"] == 1010


async def test_missing_section_errors(client, mock_api):
    mock_api.get(OBJECT).mock(return_value=httpx.Response(200, text=EML))
    with pytest.raises(ToolError, match="no purpose section"):
        await client.call_tool("get_metadata_document", {"pid": PID, "section": "purpose"})


def free_text(xml, section="methods"):
    return eml.parse(xml)[1].free_text(section)


def methods_of(inner):
    return free_text(f"<eml><dataset><methods><methodStep><description>{inner}</description></methodStep></methods></dataset></eml>")


def test_text_after_a_title_appears_once():
    assert methods_of("<section><title>Setup</title>TAILTEXT<para>p1</para></section>").text == "# Setup\nTAILTEXT\n\np1"


def test_inline_citation_text_is_kept():
    assert methods_of("<para>See <citation>Smith 2020</citation> ok</para>").text == "See Smith 2020 ok"


def test_structured_citation_is_copied_whole():
    m = methods_of("""<para>Following <citation><title>Radar calibration</title>
      <creator><individualName><givenName>Jane</givenName><surName>Smith</surName></individualName></creator>
      <pubDate>2020-03-01</pubDate><article><journal>J. Atmos. Ocean. Tech.</journal><volume>37</volume>
      <pageRange>1-9</pageRange></article></citation>.</para>""")
    assert m.text == "Following [citation 1]."
    assert m.elements["citations"] == [{
        "ref": "citation 1", "title": "Radar calibration",
        "creator": {"individualName": {"givenName": "Jane", "surName": "Smith"}}, "pubDate": "2020-03-01",
        "article": {"journal": "J. Atmos. Ocean. Tech.", "volume": "37", "pageRange": "1-9"},
    }]


def test_referenced_citation_is_followed():
    m = free_text("""<eml><dataset><methods><methodStep><description><para>As in <citation><references>cit1</references></citation>.</para></description></methodStep></methods></dataset>
      <additionalMetadata><citation id="cit1" system="x"><title>Paper</title><pubDate>2019</pubDate></citation>
      </additionalMetadata></eml>""")
    assert m.text == "As in [citation 1]."
    assert m.elements["citations"] == [{"ref": "citation 1", "title": "Paper", "pubDate": "2019"}]


def test_protocol_is_copied_whole():
    m = free_text("""<eml><dataset><methods><methodStep><description><para>Step</para></description>
      <protocol id="p1"><title>Proto</title><creator><individualName><givenName>Jane</givenName><surName>Doe</surName>
      </individualName><electronicMailAddress>j@x.org</electronicMailAddress></creator><distribution><online>
      <url function="information">https://p.org</url></online></distribution>
      <proceduralStep><description><para>Mix <emphasis>well</emphasis>, then wait.</para></description></proceduralStep>
      </protocol></methodStep></methods></dataset></eml>""")
    assert m.text == "Step\n\n[protocol 1]"
    assert m.elements["protocols"] == [{
        "ref": "protocol 1", "title": "Proto",
        "creator": {"individualName": {"givenName": "Jane", "surName": "Doe"}, "electronicMailAddress": "j@x.org"},
        "distribution": {"online": {"url": {"function": "information", "#text": "https://p.org"}}},
        "proceduralStep": {"description": "Mix well, then wait."},
    }]


def test_data_source_and_sampling_coverage_are_copied():
    m = free_text("""<eml><dataset><methods><methodStep><description><para>Step</para></description>
      <dataSource><title>Upstream ERA5 reanalysis</title></dataSource></methodStep>
      <sampling><studyExtent><coverage><geographicCoverage><geographicDescription>Toolik Lake</geographicDescription>
      </geographicCoverage></coverage></studyExtent><samplingDescription><para>Grab samples</para></samplingDescription>
      <spatialSamplingUnits><referencedEntityId>plots</referencedEntityId></spatialSamplingUnits>
      </sampling></methods></dataset></eml>""")
    assert m.text == "Step\n\n[dataSource 1]\n\n# Sampling\n\n[coverage 1]\n\nGrab samples\n\n[spatialSamplingUnits 1]"
    assert m.elements["data_sources"] == [{"ref": "dataSource 1", "title": "Upstream ERA5 reanalysis"}]
    assert m.elements["sampling_coverage"] == [{"ref": "coverage 1", "geographicCoverage": {"geographicDescription": "Toolik Lake"}}]
    assert m.elements["spatial_sampling_units"] == [{"ref": "spatialSamplingUnits 1", "referencedEntityId": "plots"}]


def test_ulink_keeps_its_url():
    assert methods_of('<para>See <ulink url="https://example.org/proto"><citetitle>the protocol</citetitle></ulink>'
                      ' for details.</para>').text == "See [the protocol](https://example.org/proto) for details."
    assert methods_of('<para><ulink url="https://x.org">https://x.org</ulink></para>').text == "https://x.org"


def test_language_values_are_separated():
    assert methods_of('<para>Hello<value xml:lang="fr">Bonjour</value></para>').text == "Hello / Bonjour"


def test_markdown_and_literal_layout_keep_their_spacing():
    text = methods_of("""<markdown>
        - a
            - nested

        ```
        x  =  1
        ```
      </markdown>
      <para><literalLayout>col1    col2
1       2</literalLayout></para>""").text
    assert text == "- a\n    - nested\n\n```\nx  =  1\n```\n\ncol1    col2\n1       2"


def test_ordered_lists_are_numbered():
    text = methods_of("<orderedlist><listitem><para>one</para></listitem><listitem><para>two</para></listitem>"
                      "</orderedlist><itemizedlist><listitem><para>x</para></listitem></itemizedlist>").text
    assert text == "1. one\n\n2. two\n\n- x"


def test_intellectual_rights_includes_licensed():
    lic = """<licensed><licenseName>CC-BY 4.0</licenseName><url>https://creativecommons.org/licenses/by/4.0/</url>
      <identifier>CC-BY-4.0</identifier></licensed>"""
    only = free_text(f"<eml><dataset>{lic}</dataset></eml>", "intellectual_rights")
    assert only.text == ""
    assert only.elements["licenses"] == [{
        "licenseName": "CC-BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/", "identifier": "CC-BY-4.0"}]
    both = free_text(f"<eml><dataset><intellectualRights><para>Free to use</para></intellectualRights>{lic}</dataset></eml>",
                     "intellectual_rights")
    assert both.text == "Free to use" and len(both.elements["licenses"]) == 1
    assert eml.parse(f"<eml><dataset>{lic}</dataset></eml>")[1].outline()["license"] == \
        "CC-BY 4.0 (https://creativecommons.org/licenses/by/4.0/)"


def test_reference_cycles_in_copied_elements_stop():
    m = methods_of('<para>See <citation id="c1"><para>as in <citation><references>c1</references></citation>'
                   '</para></citation>.</para>')
    assert m.text == "See [citation 1]."
    assert {"ref": "citation 2", "reference_cycle": "c1"} in m.elements["citations"]


def test_unresolved_reference_is_flagged():
    m = methods_of("<para>As in <citation><references>nope</references></citation>.</para>")
    assert m.elements["citations"] == [{"ref": "citation 1", "unresolved_reference": "nope"}]


def test_copies_count_toward_max_chars():
    cites = "".join(
        f"<para>Step {i} follows <citation><title>Paper number {i} on radar calibration</title>"
        f"<creator><individualName><givenName>Jane</givenName><surName>Smith{i}</surName></individualName></creator>"
        f"<pubDate>2020</pubDate></citation>.</para>"
        for i in range(60)
    )
    m = methods_of(cites)
    max_chars = 2000
    offset, text, refs = 0, "", []
    while offset is not None:
        page = m.page(offset, max_chars)
        copies = page.get("citations", [])
        assert len(page["text"]) + sum(eml._size(c) for c in copies) <= max_chars
        # Each copy is on the same page as its placeholder.
        for c in copies:
            assert f"[{c['ref']}]" in page["text"]
        text += page["text"]
        refs += [c["ref"] for c in copies]
        offset = page["next_offset"]
    assert text == m.text
    assert refs == [f"citation {i}" for i in range(1, 61)]


def test_oversized_copy_is_still_returned():
    big = "x" * 3000
    m = methods_of(f"<para>A <citation><title>{big}</title><pubDate>2020</pubDate></citation> B</para>")
    first = m.page(0, 1000)
    # The copy can't fit on any page, so the page ends before its placeholder ...
    assert first["text"] == "A " and "citations" not in first
    # ... and the next page starts with it, oversized.
    second = m.page(first["next_offset"], 1000)
    assert second["text"].startswith("[citation 1]") and second["citations"][0]["title"] == big
    assert second["next_offset"] is None
