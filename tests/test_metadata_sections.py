import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from arcticdata_mcp import eml, sampling
from arcticdata_mcp.server import create_server
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


@pytest.mark.parametrize("section", ["raw", "outline", "attributes", "coverage"])
async def test_structured_sections_are_never_summarized(client, section):
    with pytest.raises(ToolError, match="never summarized"):
        await client.call_tool("get_metadata_document", {"pid": PID, "section": section, "summarize": True})


async def test_summarize_via_sampling_on_handshake_protocol(settings, mock_api):
    mock_api.get(OBJECT).mock(return_value=httpx.Response(200, text=EML))
    requests = []

    def handler(messages, params, context):
        requests.append((messages, params))
        return "Cores drilled to 3 m, logged every 10 cm."

    async with Client(create_server(settings, require_auth=False), sampling_handler=handler, mode="legacy") as c:
        r = (await c.call_tool("get_metadata_document", {"pid": PID, "section": "methods", "summarize": True, "summary_words": 100})).structured_content
    s = r["summary"]
    assert s["text"] == "Cores drilled to 3 m, logged every 10 cm."
    assert s["generated_by"] == "client LLM via MCP sampling" and "Model-generated" in s["note"]
    assert "text" not in r
    [(messages, params)] = requests
    assert "Cores were drilled" in messages[0].content.text and "at most 100 words" in messages[0].content.text
    assert params.max_tokens == 300 and "Do not add information" in params.system_prompt


@pytest.mark.parametrize(
    ("client_kwargs", "status"),
    [
        ({"sampling_handler": lambda m, p, c: "x"}, "protocol_2026-07-28_has_no_sampling"),
        ({"mode": "legacy"}, "client_does_not_support_sampling"),
        ({"mode": "legacy", "sampling_handler": lambda m, p, c: (_ for _ in ()).throw(RuntimeError("declined"))}, "sampling_failed: declined"),
    ],
)
async def test_summarize_falls_back_to_full_text(settings, mock_api, client_kwargs, status):
    mock_api.get(OBJECT).mock(return_value=httpx.Response(200, text=EML))
    async with Client(create_server(settings, require_auth=False), **client_kwargs) as c:
        r = (await c.call_tool("get_metadata_document", {"pid": PID, "section": "methods", "summarize": True})).structured_content
    assert r["summary_status"] == status
    assert "summary" not in r and r["text"].startswith("# Field work")


async def test_short_sections_are_not_summarized(settings, mock_api):
    mock_api.get(OBJECT).mock(return_value=httpx.Response(200, text=EML))
    called = []
    async with Client(create_server(settings, require_auth=False), sampling_handler=lambda m, p, c: called.append(1) or "x", mode="legacy") as c:
        r = (await c.call_tool("get_metadata_document", {"pid": PID, "section": "abstract", "summarize": True})).structured_content
    assert r["summary_status"] == "not_needed_short_section" and not called
    assert len(LONG_STEP) > sampling.MIN_CHARS
