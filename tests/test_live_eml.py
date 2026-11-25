"""Live EML coverage check (issue #29): do recent ADC documents hold values that no section returns?

Manual only: EML and the way the ADC editor writes it change rarely, so this runs on demand (Actions tab > live >
Run workflow, or `uv run pytest -m manual`), when EML publishes a release or MetacatUI may write EML differently.

It samples the most recently uploaded current EML documents of each supported version. Every element or
attribute path holding a value that appears in no section is compared with eml.IGNORED, eml.KNOWN_GAPS and the
baseline in tests/data/eml-coverage-baseline.txt, and the test fails on any path that none of them explains.
It is a heuristic: a short value such as "1" can match unrelated text and hide a gap, and it only sees what the
sampled documents contain. tests/test_eml_schema.py is the complete check against the schemas.

To accept the current gaps as the new baseline: UPDATE_EML_BASELINE=1 uv run pytest -m manual tests/test_live_eml.py
"""

import os
from pathlib import Path

import pytest
from eml_coverage import explained, missing_paths

from arcticdata_mcp import eml
from arcticdata_mcp.client import DataONEClient
from arcticdata_mcp.config import Settings

pytestmark = [pytest.mark.live, pytest.mark.manual]
BASELINE = Path(__file__).parent / "data" / "eml-coverage-baseline.txt"
DOCUMENTS_PER_VERSION = 15


def read_baseline() -> set[str]:
    lines = BASELINE.read_text().splitlines() if BASELINE.exists() else []
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def write_baseline(paths: set[str]) -> None:
    header = (
        "# Paths in recent ADC EML documents whose values reach no section of get_metadata_document and are not\n"
        "# on eml.IGNORED or eml.KNOWN_GAPS. Written by tests/test_live_eml.py with UPDATE_EML_BASELINE=1.\n"
        "# Use it for false alarms, such as values the parser rewrites so the text no longer matches;\n"
        "# real gaps go on eml.KNOWN_GAPS.\n"
    )
    BASELINE.write_text(header + "".join(f"{p}\n" for p in sorted(paths)))


async def recent_documents(client: DataONEClient, format_id: str) -> list[str]:
    """PIDs of the most recently uploaded current documents in ``format_id``. A PID never changes, so new
    uploads are the only place a change in how ADC writes EML can show up."""
    data = await client.solr(
        [
            ("q", f'formatId:"{format_id}"'),
            ("fq", "-obsoletedBy:*"),
            ("sort", "dateUploaded desc"),
            ("fl", "id"),
            ("rows", DOCUMENTS_PER_VERSION),
        ]
    )
    return [d["id"] for d in data["response"]["docs"]]


async def test_recent_documents_reach_a_section():
    client = DataONEClient(Settings())
    found: dict[str, tuple[str, str]] = {}  # path -> (an example pid, an example value)
    checked = 0
    try:
        # For EML, the formatId is the document's namespace.
        for format_id in eml.SUPPORTED_NAMESPACES:
            pids = await recent_documents(client, format_id)
            assert pids, f"no current documents in {format_id}"
            for pid in pids:
                text = await client.get_text(client.object_url(pid))
                if eml.parse(text)[1] is None:  # an EML document describing software or a protocol
                    continue
                checked += 1
                for path, values in missing_paths(text).items():
                    if not explained(path):
                        found.setdefault(path, (pid, values[0]))
    finally:
        await client.aclose()
    assert checked, "no EML datasets were checked"
    if os.environ.get("UPDATE_EML_BASELINE"):
        write_baseline(set(found))
        return
    new = {p: v for p, v in found.items() if p not in read_baseline()}
    assert not new, (
        f"Values that reach no section, in {checked} recent EML documents. Handle them in eml.py, add them to "
        "eml.IGNORED or eml.KNOWN_GAPS, or if they are false alarms, to the baseline (UPDATE_EML_BASELINE=1):\n"
        + "\n".join(f"{p}  e.g. {v[:60]!r} in {pid}" for p, (pid, v) in sorted(new.items()))
    )
