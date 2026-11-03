"""compare_eml_vs_data: check a dataset's EML against what its data files contain."""

from __future__ import annotations

import time
from typing import Annotated, Any, Literal
from urllib.parse import unquote

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from arcticdata_mcp import compare, eml, solr
from arcticdata_mcp.tools.common import READ_ONLY, ToolContext
from arcticdata_mcp.tools.profile import profile_object


def pid_from_url(url: str | None) -> str | None:
    """The DataONE identifier inside an EML distribution URL, e.g. ``.../resolve/urn%3Auuid%3A...``."""
    if not url:
        return None
    text = unquote(url.strip())
    for marker in ("/resolve/", "/object/"):
        if marker in text:
            return text.split(marker, 1)[1].split("?")[0].strip() or None
    return text if text.startswith(("urn:", "doi:", "uuid:")) else None


def spread(items: list[Any], n: int) -> list[Any]:
    """Up to ``n`` items spread evenly over the list, always including the first."""
    if n >= len(items):
        return list(items)
    if n == 1:
        return items[:1]
    return [items[round(i * (len(items) - 1) / (n - 1))] for i in range(n)]


def interleave(lists: list[list[Any]]) -> list[Any]:
    """Round-robin over several lists, so a cap on the total takes from every group before any group's second file."""
    out = []
    for i in range(max((len(x) for x in lists), default=0)):
        out += [x[i] for x in lists if i < len(x)]
    return out


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    client = ctx.client
    settings = ctx.settings
    limits = settings.profile
    cfg = settings.compare

    async def resolve(pid: str) -> tuple[str, str | None, list[str], str | None]:
        """(metadata PID, the data PID the caller asked about if it was one, the package's resource maps, and the
        PID that replaced the metadata record if it has been superseded)."""
        data = await client.solr([
            ("q", f"id:{solr.phrase(pid)}"), ("fl", "id,formatType,isDocumentedBy,resourceMap,obsoletedBy"), ("rows", 1),
        ])
        docs = data.get("response", {}).get("docs", [])
        if not docs:
            raise ToolError(f"No public object with id '{pid}' found in the index. Try find_dataset_by_identifier.")
        doc = docs[0]
        kind = doc.get("formatType")
        if kind == "METADATA":
            return pid, None, doc.get("resourceMap") or [], doc.get("obsoletedBy")
        if kind == "DATA":
            clause = " OR ".join(f"id:{solr.phrase(x)}" for x in doc.get("isDocumentedBy") or [])
        elif kind == "RESOURCE":
            clause = f"resourceMap:{solr.phrase(pid)}"
        else:
            raise ToolError(f"'{pid}' is a {kind or 'unknown'} object; give a dataset (metadata) PID or a data file PID.")
        if not clause:
            raise ToolError(f"'{pid}' is not documented by any metadata record.")
        metas: list[dict[str, Any]] = []
        for current_only in (True, False):  # prefer the current record; fall back to a superseded one that was asked for
            found = await client.solr([
                ("q", clause), ("fq", "formatType:METADATA"), *([("fq", "-obsoletedBy:*")] if current_only else []),
                ("fl", "id,resourceMap,obsoletedBy"), ("rows", 1),
            ])
            if metas := found.get("response", {}).get("docs", []):
                break
        if not metas:
            raise ToolError(f"No metadata record found for '{pid}'.")
        return metas[0]["id"], (pid if kind == "DATA" else None), metas[0].get("resourceMap") or [], metas[0].get("obsoletedBy")

    async def package_files(meta_pid: str, resource_maps: list[str]) -> dict[str, list[str]]:
        """File name -> PIDs for the first page of the package's data files; used when an entity has no usable URL."""
        clause = " OR ".join([f"isDocumentedBy:{solr.phrase(meta_pid)}"] + [f"resourceMap:{solr.phrase(r)}" for r in resource_maps])
        data = await client.solr([
            ("q", clause), ("fq", "formatType:DATA"), ("fq", "-obsoletedBy:*"), ("fl", "id,fileName"), ("rows", solr.MAX_ROWS),
        ])
        names: dict[str, list[str]] = {}
        for d in data.get("response", {}).get("docs", []):
            names.setdefault(d.get("fileName") or "", []).append(d["id"])
        return names

    @mcp.tool(annotations=READ_ONLY, tags={"files", "metadata-quality"})
    async def compare_eml_vs_data(
        pid: Annotated[str, Field(description=(
            "Dataset (metadata) PID or DOI. A data file PID is also accepted: only that file is compared, "
            "against the current metadata record that documents it."
        ))],
        files_per_schema: Annotated[int | Literal["all"] | None, Field(description=(
            "Data files to profile for each group of entities that share an attribute list, spread evenly over the "
            "group. A number samples that many (default 3, see get_server_config); 'all' profiles every file, up to the "
            "server's per-call file cap."
        ))] = None,
        scan: Annotated[Literal["sample", "full"], Field(description=(
            "How much of each file profile_file reads: 'sample' (fast) or 'full'."
        ))] = "sample",
        schema_id: Annotated[str | None, Field(description=(
            "Only compare this group of entities (a schema_id from get_metadata_document's entity_groups)."
        ))] = None,
    ) -> dict[str, Any]:
        """Compare a dataset's EML with what its data files actually contain, and list the mismatches.

        Reads the EML attribute lists, units, missing-value codes, bounds, date formats and coverage, profiles a
        sample of the data files (see profile_file for the formats), and reports findings: undocumented columns,
        documented columns missing from the data, type, unit, date-format and missing-value disagreements, values
        outside declared bounds, and data outside the declared geographic or temporal coverage.
        Each finding has a severity (error, warning, info), a confidence in the data side, the EML and data values, and
        a `suggested_edit` naming the EML element to change, so the list can be turned into proposed edits. Units
        guessed from CSV column names are low confidence; units written in a NetCDF file are high.
        Files are matched to EML entities by the PID in each entity's URL, then by file name. Raster and
        unsupported files are listed but only rasters feed the coverage check.
        Example: compare_eml_vs_data(pid="doi:10.18739/A2BG2HC7C", files_per_schema=2)
        """
        if isinstance(files_per_schema, int) and files_per_schema < 1:
            raise ToolError("files_per_schema must be at least 1, or 'all'.")
        per_group = files_per_schema if files_per_schema is not None else cfg.files_per_schema
        started = time.monotonic()

        meta_pid, only_pid, resource_maps, superseded_by = await resolve(pid)
        doc = eml.parse(await client.get_text(client.object_url(meta_pid)))
        if doc is None:
            raise ToolError("This metadata record is not EML (for example ISO 19115), so it cannot be compared yet.")
        notes: list[str] = []
        if superseded_by:
            notes.append(f"This metadata record has been superseded by '{superseded_by}'; the version asked for was compared.")

        # -- choose files ----------------------------------------------------------------------------------------
        groups: dict[str, dict[str, Any]] = {}
        for e in doc.entities:
            key = e.get("schema_id") or f"none:{e['type']}"
            g = groups.setdefault(key, {"key": key, "schema_id": e.get("schema_id"), "entity_type": e["type"],
                                        "entities": [], "n_attributes": e["n_attributes"]})
            g["entities"].append({**e, "pid": pid_from_url(e.get("url"))})
        if schema_id:
            if schema_id not in groups:
                raise ToolError(f"Unknown schema_id '{schema_id}'. This document has: {', '.join(doc.schemas) or 'none'}.")
            groups = {schema_id: groups[schema_id]}
        if only_pid:
            groups = {k: {**g, "entities": [e for e in g["entities"] if e["pid"] == only_pid]} for k, g in groups.items()}
            groups = {k: g for k, g in groups.items() if g["entities"]}
            if not groups:
                raise ToolError(f"The EML of '{meta_pid}' has no entity whose URL points at '{only_pid}'.")

        if any(e["pid"] is None for g in groups.values() for e in g["entities"]):
            by_name = await package_files(meta_pid, resource_maps)
            for g in groups.values():
                for e in g["entities"]:
                    if e["pid"] is None:
                        for name in (e.get("file"), e.get("name")):
                            if name and len(by_name.get(name, [])) == 1:
                                e["pid"] = by_name[name][0]
                                break

        picked: dict[str, list[dict[str, Any]]] = {}
        for key, g in groups.items():
            usable = [e for e in g["entities"] if e["pid"]]
            picked[key] = usable if per_group == "all" else spread(usable, per_group)
        queue = interleave(list(picked.values()))
        order = [(key, e) for key, es in picked.items() for e in es]
        if len(queue) > cfg.max_files:
            notes.append(
                f"{len(queue)} files were selected but at most {cfg.max_files} are profiled per call; the rest were "
                "skipped, taking from every group before any group's second file."
            )
        owner = {id(e): key for key, e in order}
        chosen = queue[: cfg.max_files]

        # -- profile ---------------------------------------------------------------------------------------------
        results: dict[str, list[dict[str, Any]]] = {key: [] for key in groups}
        file_rows: dict[str, list[dict[str, Any]]] = {key: [] for key in groups}
        stalled: set[str] = set()  # groups whose files timed out; the rest are likely as slow
        for e in chosen:
            key = owner[id(e)]
            row = {"pid": e["pid"], "entity": e.get("name") or e.get("file")}
            if time.monotonic() - started > cfg.budget:
                row["status"] = "skipped: time budget used"
            elif key in stalled:
                row["status"] = "skipped: an earlier file in this group timed out"
            else:
                try:
                    profile = await profile_object(client, limits, e["pid"], scan)
                except ToolError as exc:
                    row.update(status="error", error=str(exc)[:300])
                    if "longer than" in str(exc):
                        stalled.add(key)
                else:
                    row["fileName"] = profile.get("fileName")
                    if not profile.get("supported"):
                        row.update(status="unsupported", reason=profile.get("reason"))
                    else:
                        row.update(status="profiled", kind=profile["kind"], complete=profile["complete"])
                        if "rows_profiled" in profile:
                            row["rows_profiled"] = profile["rows_profiled"]
                        results[key].append(profile)
            file_rows[key].append(row)
        statuses = [r["status"] for rows in file_rows.values() for r in rows]
        if "skipped: time budget used" in statuses:
            notes.append(f"The {cfg.budget:.0f}-second budget ran out before every selected file was profiled.")
        if stalled:
            notes.append(
                f"Files timed out after {limits.timeout:.0f} seconds (large NetCDF-4 files read over HTTP are slow); the "
                "rest of those groups were skipped. Raise profile.timeout or compare a smaller dataset."
            )
        if any("NotAuthorized" in str(r.get("error", "")) for rows in file_rows.values() for r in rows):
            notes.append("Some files are not publicly readable, so they could not be profiled.")
        if chosen and "profiled" not in statuses:
            notes.append("No file could be profiled, so only the EML was read; see groups[].files for the reasons.")
        if sum(1 for g in groups.values() for e in g["entities"] if not e["pid"]):
            notes.append("Some entities have no URL or file name that identifies a data file in the archive.")

        # -- compare ---------------------------------------------------------------------------------------------
        findings: list[dict[str, Any]] = []
        if not doc.entities:
            findings.append(compare.finding(
                "no_entities", "warning", "high",
                "The EML describes no data entities (dataTable, otherEntity, ...), so there is nothing to compare; "
                "the package may still contain files.", edit=compare.edit("add", "dataset/dataTable or otherEntity")))
        for key, g in groups.items():
            profiles = [p for p in results[key] if p["kind"] in ("table", "netcdf")]
            if not profiles:
                continue
            n_entities = len(g["entities"])
            facts = compare.merge_facts([compare.facts_from_profile(p) for p in profiles])
            truncated = any(p.get("columns_total", 0) > len(p.get("columns", [])) for p in profiles)
            if g["schema_id"]:
                found = compare.compare_group(
                    doc.schemas[g["schema_id"]], facts, kind=profiles[0]["kind"], n_files=len(profiles),
                    schema_id=g["schema_id"], sentinels=limits.sentinels, truncated=truncated,
                )
                for f in found:
                    f["n_entities_in_schema"] = n_entities
                findings += found
            else:
                drafts = [compare.draft(f) for f in facts if not f.get("unprofiled")]
                findings.append(compare.finding(
                    "no_attribute_list", "warning", "high",
                    f"{n_entities} {g['entity_type']} entit{'y has' if n_entities == 1 else 'ies have'} no attribute list; "
                    f"the {len(profiles)} file(s) profiled contain {len(drafts)} column(s).",
                    data=drafts, files=[p.get("fileName") for p in profiles][:5],
                    edit=compare.edit("add", "attributeList/attribute", drafts)))
        for key, g in groups.items():
            if g["schema_id"] and results[key] and all(p["kind"] == "raster" for p in results[key]):
                notes.append(f"Schema {g['schema_id']}: attribute checks are not done for raster files.")

        all_profiles = [p for ps in results.values() for p in ps]
        findings += compare.compare_coverage(doc.coverage(), [compare.extent_of(p) for p in all_profiles], cfg.bbox_tolerance)
        findings = compare.sort_findings(findings)
        if len(findings) > compare.MAX_FINDINGS:
            notes.append(f"Only the first {compare.MAX_FINDINGS} of {len(findings)} findings are shown.")
            findings = findings[: compare.MAX_FINDINGS]
        if scan == "sample" and all_profiles:
            notes.append(
                "scan='sample' reads part of each file, so ranges, extents and counts cover only that part: findings "
                "that data fall outside declared bounds or coverage are certain, the others reflect the sample. "
                "Use scan='full' to read more."
            )

        counts = {s: sum(1 for f in findings if f["severity"] == s) for s in compare.SEVERITY_ORDER}
        return {
            "pid": meta_pid,
            "title": (doc.dataset.findtext("title") or "").strip() or None,
            "scan": scan,
            "files_per_schema": per_group,
            "summary": {
                "findings": counts,
                "entities": len(doc.entities),
                "schemas": len(doc.schemas),
                "files_profiled": statuses.count("profiled"),
                "files_failed": statuses.count("error"),
                "files_unsupported": statuses.count("unsupported"),
                "files_skipped": sum(1 for s in statuses if s.startswith("skipped")),
            },
            "groups": [
                {"schema_id": g["schema_id"], "entity_type": g["entity_type"], "n_entities": len(g["entities"]),
                 "n_attributes": g["n_attributes"], "files": file_rows[key]}
                for key, g in groups.items()
            ],
            "findings": findings,
            "notes": notes,
        }
