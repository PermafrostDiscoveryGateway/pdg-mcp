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
    text = eml.parse(EML)[1].free_text("methods")
    assert text.startswith("# Field work")
    assert "Cores were drilled" in text and "- Item one" in text
    assert "# Sampling" in text and "Random plots" in text
    assert "Cited dataset" not in text


def test_free_text_section_missing():
    doc = eml.parse(EML)[1]
    assert doc.free_text("purpose") is None
    assert doc.free_text("abstract") == "Short abstract about permafrost cores."


async def test_non_eml_documents_are_rejected(client, mock_api):
    mock_api.get(OBJECT).mock(return_value=httpx.Response(200, text="<MD_Metadata/>"))
    with pytest.raises(ToolError, match="only available for EML"):
        await client.call_tool("get_metadata_document", {"pid": PID, "section": "abstract"})


async def test_section_paging(client, mock_api):
    mock_api.get(OBJECT).mock(return_value=httpx.Response(200, text=EML))
    first = (await client.call_tool("get_metadata_document", {"pid": PID, "section": "methods", "max_chars": 1000})).structured_content
    assert first["next_offset"] == 1000 and len(first["text"]) == 1000
    rest = (await client.call_tool("get_metadata_document", {"pid": PID, "section": "methods", "offset": 1000, "max_chars": 100_000})).structured_content
    assert first["text"] + rest["text"] == eml.parse(EML)[1].free_text("methods")
    assert rest["next_offset"] is None


async def test_raw_document_paging(client, mock_api):
    mock_api.get(OBJECT).mock(return_value=httpx.Response(200, text=EML))
    r = (await client.call_tool("get_metadata_document", {"pid": PID, "section": "raw", "offset": 10, "max_chars": 1000})).structured_content
    assert r["document"] == EML[10:1010] and r["next_offset"] == 1010


async def test_missing_section_errors(client, mock_api):
    mock_api.get(OBJECT).mock(return_value=httpx.Response(200, text=EML))
    with pytest.raises(ToolError, match="no purpose section"):
        await client.call_tool("get_metadata_document", {"pid": PID, "section": "purpose"})
