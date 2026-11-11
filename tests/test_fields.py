"""The Solr field names we hard-code are all listed for solr.check_fields, and the check catches a rename."""

import asyncio
import logging
import re
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

import arcticdata_mcp
from arcticdata_mcp import solr
from arcticdata_mcp.server import create_server
from arcticdata_mcp.tools import fields_in_use
MN = "https://arcticdata.io/metacat/d1/mn/v2"

SRC = Path(arcticdata_mcp.__file__).parent
# A field name written straight into a query string, as in f"id:{...}", "-obsoletedBy:*" or "size:[".
INLINE_FIELD = re.compile(r"(?<![\w.{}])([A-Za-z_]\w*):(?=[{\[*(])")
FLAGS = {"returned": "returnable", "searched": "searchable", "sorted_by": "sortable"}


def index_for(use: solr.FieldUse) -> dict:
    """A test double of the index in which every field we use exists with every flag set."""
    names = use.returned | use.searched | use.sorted_by
    return {n: {"name": n, "type": "string", **dict.fromkeys(solr.FIELD_FLAGS, True)} for n in names}


def field_list_xml(index: dict) -> str:
    fields = "".join(
        f"<queryField><name>{f['name']}</name>"
        + "".join(f"<{flag}>{str(f[flag]).lower()}</{flag}>" for flag in solr.FIELD_FLAGS)
        + "</queryField>"
        for f in index.values()
    )
    return f"<queryEngineDescription><name>solr</name>{fields}</queryEngineDescription>"


def test_fields_in_use_covers_the_known_lists():
    use = fields_in_use()
    assert {"id", "title", "checksum", "collectionQuery", "obsoletes"} <= use.returned
    assert {"keywordsText", "prov_wasDerivedFrom", "obsoletedBy", "funderIdentifier", "isDocumentedBy"} <= use.searched
    assert use.sorted_by == {"dateUploaded", "datePublished", "titlestr", "size"}
    assert solr.check_fields(index_for(use), use) == []


@pytest.mark.parametrize("group", FLAGS)
def test_renaming_any_field_fails_the_check_with_its_name(group):
    use = fields_in_use()
    for name in sorted(getattr(use, group)):
        index = index_for(use)
        index[f"{name}Renamed"] = index.pop(name)
        problems = solr.check_fields(index, use)
        assert problems and all(p.startswith(f"{name}: not in the index") for p in problems), (name, problems)
        assert f"{name}: not in the index (we need it {FLAGS[group]})" in problems


@pytest.mark.parametrize("path", ["solr.py", "tools/search.py", "tools/datasets.py", "tools/portals.py"])
def test_field_names_written_into_queries_are_listed(path):
    """Catches a field added to a query string but not to a QUERY_FIELDS list, which the check would then miss."""
    found = set(INLINE_FIELD.findall((SRC / path).read_text()))
    assert found, f"no query fields found in {path}; has the pattern stopped matching?"
    assert found <= fields_in_use().searched, f"add to QUERY_FIELDS in {path}: {sorted(found - fields_in_use().searched)}"


async def test_startup_check_warns_about_a_renamed_field(settings, mock_api, caplog):
    index = index_for(fields_in_use())
    index["keywordsTextV2"] = {**index.pop("keywordsText"), "name": "keywordsTextV2"}
    route = mock_api.get(f"{MN}/query/solr").mock(return_value=httpx.Response(200, text=field_list_xml(index)))
    with caplog.at_level(logging.WARNING, logger="arcticdata_mcp.server"):
        async with Client(create_server(settings, require_auth=False, check_index_fields=True)) as c:
            for _ in range(100):
                if route.called and caplog.records:
                    break
                await asyncio.sleep(0.01)
    assert [r.getMessage() for r in caplog.records] == [
        "Solr field name out of sync with the index: keywordsText: not in the index (we need it searchable)"
    ]


async def test_startup_check_failure_does_not_stop_the_server(settings, mock_api, caplog):
    mock_api.get(f"{MN}/query/solr").mock(side_effect=httpx.ConnectError("down"))
    with caplog.at_level(logging.WARNING, logger="arcticdata_mcp.server"):
        async with Client(create_server(settings, require_auth=False, check_index_fields=True)) as c:
            for _ in range(100):
                if caplog.records:
                    break
                await asyncio.sleep(0.01)
            assert await c.list_tools()
    assert "Could not check Solr field names" in caplog.text
