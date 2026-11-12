"""Compare free-text and ontology (PURL) variable search against the live Arctic Data Center index.

For each benchmark term this checks that search_by_variable resolves it to the expected ECSO concept and
prints the dataset counts from free text (search_by_measured_variable), the PURL alone
(search_by_semantic_annotation) and the combined search. Counts are latest versions only.

    uv run python scripts/benchmark_variable_search.py
"""

from __future__ import annotations

import asyncio
import sys

from fastmcp import Client

from arcticdata_mcp.config import Settings
from arcticdata_mcp.server import create_server

ODO = "http://purl.dataone.org/odo/"

# (term as a user might write it, expected ECSO concept or None when ECSO has no good match)
BENCHMARK: list[tuple[str, str | None]] = [
    ("soil depth", f"{ODO}ECSO_00001207"),
    ("soil temperature", f"{ODO}ECSO_00001230"),
    ("active layer thickness", f"{ODO}ECSO_00001210"),
    ("snow depth", f"{ODO}ECSO_00001205"),
    ("air temperature", f"{ODO}ECSO_00001225"),
    ("methane flux", f"{ODO}ECSO_00000582"),
    ("permafrost temperature", None),
]


async def run() -> int:
    failures = 0
    header = f"{'term':<24} {'resolved concept':<34} {'ok':<3} {'free text':>9} {'PURL':>6} {'combined':>8}"
    print(header)
    print("-" * len(header))
    async with Client(create_server(Settings(), require_auth=False)) as c:
        for term, expected in BENCHMARK:
            combined = (await c.call_tool("search_by_variable", {"variable": term, "rows": 0})).structured_content
            free = (await c.call_tool("search_by_measured_variable", {"variable": term, "rows": 0})).structured_content
            used = [u["uri"] for u in combined["concepts_used"]]
            ok = (expected in used) if expected else not used
            failures += not ok
            purl = combined["counts"]["annotation"] if used else 0
            label = ", ".join(u["label"] or u["uri"] for u in combined["concepts_used"]) or "(free-text fallback)"
            print(
                f"{term:<24} {label[:34]:<34} {'yes' if ok else 'NO':<3} {free['numFound']:>9} {purl:>6} {combined['numFound']:>8}"
            )
    return failures


def main() -> None:
    failures = asyncio.run(run())
    if failures:
        print(f"\n{failures} term(s) did not resolve to the expected concept")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
