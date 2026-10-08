"""Dataset- and package-level tools: details, file listings, metadata documents, ORE maps, citations, versions."""

from __future__ import annotations

import re
import defusedxml.ElementTree as ET
from html.parser import HTMLParser
from typing import Annotated, Any, Literal
from urllib.parse import unquote

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from arcticdata_mcp import downloads, solr
from arcticdata_mcp.client import quote_pid
from arcticdata_mcp.tools.common import READ_ONLY, Pid, Rows, Start, ToolContext

NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "ore": "http://www.openarchives.org/ore/terms/",
    "cito": "http://purl.org/spar/cito/",
    "dcterms": "http://purl.org/dc/terms/",
    "prov": "http://www.w3.org/ns/prov#",
}
BAGIT_FORMAT = "application%2Fbagit-1.0"


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag in {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "dt", "dd", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        return re.sub(r"\n\s*\n+", "\n\n", raw).strip()


def _pid_from_uri(uri: str) -> str:
    """ORE maps reference objects as https://cn.dataone.org/cn/v2/resolve/<quoted pid>."""
    uri = uri.split("#")[0]
    if "/resolve/" in uri:
        return unquote(uri.split("/resolve/", 1)[1])
    if "/object/" in uri:
        return unquote(uri.split("/object/", 1)[1])
    return uri


def parse_resource_map(xml_text: str) -> dict[str, Any]:
    root = ET.fromstring(xml_text)
    about = f"{{{NS['rdf']}}}about"
    resource = f"{{{NS['rdf']}}}resource"
    aggregated: set[str] = set()
    documents: dict[str, list[str]] = {}
    prov: list[dict[str, str]] = []
    aggregation = None
    for desc in root.findall("rdf:Description", NS):
        subj = desc.get(about, "")
        for child in desc:
            obj = child.get(resource)
            tag = child.tag
            if tag == f"{{{NS['ore']}}}aggregates" and obj:
                aggregation = aggregation or _pid_from_uri(subj)
                aggregated.add(_pid_from_uri(obj))
            elif tag == f"{{{NS['ore']}}}isAggregatedBy":
                aggregated.add(_pid_from_uri(subj))
            elif tag == f"{{{NS['cito']}}}documents" and obj:
                documents.setdefault(_pid_from_uri(subj), []).append(_pid_from_uri(obj))
            elif tag.startswith(f"{{{NS['prov']}}}") and obj:
                prov.append({"subject": _pid_from_uri(subj), "predicate": "prov:" + tag.split("}")[1], "object": _pid_from_uri(obj)})
    metadata = sorted(documents)
    return {
        "aggregation": aggregation,
        "metadata_pids": metadata,
        "members": sorted(aggregated),
        "documents": documents,
        "provenance": prov,
    }


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    client = ctx.client
    settings = ctx.settings

    async def solr_doc(pid: str, fl: str = "*") -> dict[str, Any]:
        data = await client.solr([("q", f"id:{solr.phrase(pid)}"), ("fl", fl), ("rows", 1)])
        docs = data.get("response", {}).get("docs", [])
        if not docs:
            raise ToolError(f"No public object with id '{pid}' found in the index. Try find_dataset_by_identifier.")
        return docs[0]

    async def resource_map_pid(pid: str) -> str:
        doc = await solr_doc(pid, "id,formatType,resourceMap")
        if doc.get("formatType") == "RESOURCE":
            return doc["id"]
        maps = doc.get("resourceMap") or []
        if not maps:
            raise ToolError(f"'{pid}' is not part of any data package (no resourceMap).")
        return maps[0]

    @mcp.tool(annotations=READ_ONLY, tags={"dataset"})
    async def get_dataset_details(
        pid: Pid,
        fields: Annotated[str | None, Field(description="Optional comma-separated Solr fields to return (default: all indexed fields).")] = None,
    ) -> dict[str, Any]:
        """Return the full indexed metadata record for a dataset (title, abstract, authors, coverage, funding,
        attributes, license, related packages, ...).

        Example: get_dataset_details(pid="doi:10.18739/A29K4B")
        """
        doc = await solr_doc(pid, fields or "*")
        doc["landing_url"] = client.landing_url(pid)
        return doc

    @mcp.tool(annotations=READ_ONLY, tags={"dataset", "files"})
    async def list_dataset_files(
        pid: Annotated[str, Field(description="Metadata PID (e.g. DOI) or resource-map PID of the dataset.")],
        rows: Rows = 100,
        start: Start = 0,
        file_name: Annotated[str | None, Field(description="Optional file-name filter (wildcards allowed, e.g. '*.csv').")] = None,
    ) -> dict[str, Any]:
        """List the data files in a dataset package: file name, size, format, checksum and direct download URL.

        Use download_file / preview_file with a returned id to retrieve content.
        Example: list_dataset_files(pid="doi:10.18739/A29K4B", file_name="*.csv")
        """
        doc = await solr_doc(pid, "id,formatType,resourceMap")
        if doc.get("formatType") == "RESOURCE":
            clause = f"resourceMap:{solr.phrase(pid)}"
        else:
            parts = [f"isDocumentedBy:{solr.phrase(pid)}"]
            parts += [f"resourceMap:{solr.phrase(rm)}" for rm in doc.get("resourceMap") or []]
            clause = " OR ".join(parts)
        q = [clause]
        if file_name:
            q.append(f"fileName:{solr.term(file_name)}")
        res = await solr.search(client, q, rows=rows, start=start, sort="title", format_type="DATA", latest_only=True)
        res["results"].sort(key=lambda d: d.get("fileName") or "")
        res["total_size_bytes_this_page"] = sum(d.get("size") or 0 for d in res["results"])
        return res

    @mcp.tool(annotations=READ_ONLY, tags={"dataset", "metadata"})
    async def get_metadata_document(
        pid: Annotated[str, Field(description="Metadata PID (e.g. DOI).")],
        max_chars: Annotated[int, Field(ge=1000, le=2_000_000, description="Truncate the XML after this many characters.")] = 200_000,
    ) -> dict[str, Any]:
        """Fetch the raw science-metadata document (usually EML XML, sometimes ISO 19115) for a dataset.

        Contains full methods, attribute definitions, units, coverage, and contacts.
        """
        text = await client.get_text(client.object_url(pid))
        return {
            "pid": pid,
            "chars": len(text),
            "truncated": len(text) > max_chars,
            "document": text[:max_chars],
        }

    @mcp.tool(annotations=READ_ONLY, tags={"dataset", "metadata"})
    async def get_metadata_html(
        pid: Annotated[str, Field(description="Metadata PID (e.g. DOI).")],
        max_chars: Annotated[int, Field(ge=1000, le=500_000, description="Truncate after this many characters.")] = 50_000,
    ) -> dict[str, Any]:
        """Render a dataset's metadata through the MNView 'metacatui' view (GET /views/metacatui/{pid}),
        returned as readable plain text. A human-friendly alternative to the raw XML.
        """
        html = await client.get_text(client.mn(f"views/metacatui/{quote_pid(pid)}"))
        parser = _TextExtractor()
        parser.feed(html)
        text = parser.text()
        return {"pid": pid, "truncated": len(text) > max_chars, "text": text[:max_chars], "landing_url": client.landing_url(pid)}

    @mcp.tool(annotations=READ_ONLY, tags={"dataset", "package"})
    async def get_resource_map(
        pid: Annotated[str, Field(description="Resource map PID, or any metadata/data PID in the package.")],
        include_raw: Annotated[bool, Field(description="Also return the raw RDF/XML.")] = False,
    ) -> dict[str, Any]:
        """Fetch and parse the OAI-ORE resource map that defines a data package: its members,
        which metadata documents describe which files, and any PROV provenance links.
        """
        rm = await resource_map_pid(pid)
        xml_text = await client.get_text(client.object_url(rm))
        parsed = parse_resource_map(xml_text)
        out = {"resource_map_pid": rm, **parsed, "member_count": len(parsed["members"])}
        if include_raw:
            out["raw"] = xml_text
        return out

    @mcp.tool(annotations=READ_ONLY, tags={"dataset", "package", "download"})
    async def get_package_download_info(
        pid: Annotated[str, Field(description="Resource map PID, or the dataset's metadata PID.")],
        save_to_server: Annotated[bool, Field(description="Stream the zip to the server's download directory (only if enabled in config).")] = False,
    ) -> dict[str, Any]:
        """Get the URL to download an entire dataset package as a BagIt zip (MNPackage GET /packages),
        plus the estimated total size. Optionally save the zip on the MCP server host.

        Packages can be very large; check the size before downloading.
        """
        rm = await resource_map_pid(pid)
        data = await client.solr([
            ("q", f"resourceMap:{solr.phrase(rm)}"), ("fl", "size"), ("rows", 0),
            ("stats", "true"), ("stats.field", "size"),
        ])
        stats = data.get("stats", {}).get("stats_fields", {}).get("size") or {}
        url = client.mn(f"packages/{BAGIT_FORMAT}/{quote_pid(rm)}")
        info: dict[str, Any] = {
            "resource_map_pid": rm,
            "package_url": url,
            "format": "application/bagit-1.0 (zip)",
            "member_count": data.get("response", {}).get("numFound"),
            "estimated_total_bytes": int(stats["sum"]) if stats.get("sum") is not None else None,
            "how_to_download": f"curl -L -o package.zip '{url}'",
            "note": "The server builds the zip on request; large packages can take minutes.",
        }
        if save_to_server:
            info.update(await downloads.save_to_server(client, settings, url, f"{rm}.zip"))
        return info

    @mcp.tool(annotations=READ_ONLY, tags={"dataset"})
    async def get_dataset_citation(
        pid: Annotated[str, Field(description="Metadata PID (e.g. DOI).")],
        style: Annotated[Literal["text", "bibtex"], Field(description="Citation format.")] = "text",
    ) -> dict[str, Any]:
        """Build a citation for a dataset from its indexed metadata (authors, year, title, repository, DOI)."""
        doc = await solr_doc(pid, "id,seriesId,title,origin,pubDate,datePublished,dateUploaded")
        authors: list[str] = doc.get("origin") or []
        date = doc.get("datePublished") or doc.get("pubDate") or doc.get("dateUploaded") or ""
        year = date[:4] or "n.d."
        title = (doc.get("title") or "").strip().rstrip(".")
        ident = doc["id"]
        doi = ident[4:] if ident.startswith("doi:") else None
        if style == "bibtex":
            key = re.sub(r"\W+", "", (authors[0].split()[-1] if authors else "arctic")) + year
            lines = [
                f"@misc{{{key},",
                f"  author = {{{' and '.join(authors)}}},",
                f"  title = {{{title}}},",
                f"  year = {{{year}}},",
                "  publisher = {Arctic Data Center},",
            ]
            lines.append(f"  doi = {{{doi}}}," if doi else f"  note = {{{ident}}},")
            lines.append(f"  url = {{{'https://doi.org/' + doi if doi else client.landing_url(ident)}}}")
            lines.append("}")
            citation = "\n".join(lines)
        else:
            if len(authors) > 1:
                who = ", ".join(authors[:-1]) + ", and " + authors[-1]
            else:
                who = authors[0] if authors else "Anonymous"
            citation = f"{who}. {year}. {title}. Arctic Data Center. {ident if doi is None else 'doi:' + doi}."
        return {"pid": ident, "style": style, "citation": citation}

    @mcp.tool(annotations=READ_ONLY, tags={"dataset", "versions"})
    async def get_version_history(
        pid: Annotated[str, Field(description="Any version's PID, or a series ID.")],
    ) -> dict[str, Any]:
        """Return the full version chain of an object (following obsoletes/obsoletedBy), oldest first,
        and identify the latest version.
        """
        fl = "id,seriesId,obsoletes,obsoletedBy,dateUploaded,title,formatType"
        data = await client.solr([
            ("q", f"id:{solr.phrase(pid)} OR seriesId:{solr.phrase(pid)}"),
            ("fl", fl), ("rows", 100), ("sort", "dateUploaded asc"),
        ])
        docs = {d["id"]: d for d in data.get("response", {}).get("docs", [])}
        if not docs:
            raise ToolError(f"No public object with id or seriesId '{pid}'")
        seen = dict(docs)
        frontier = list(docs.values())
        for _ in range(100):
            nxt = {x for d in frontier for x in (d.get("obsoletes"), d.get("obsoletedBy")) if x and x not in seen}
            if not nxt:
                break
            query = " OR ".join(f"id:{solr.phrase(x)}" for x in nxt)
            res = await client.solr([("q", query), ("fl", fl), ("rows", len(nxt))])
            frontier = res.get("response", {}).get("docs", [])
            for d in frontier:
                seen[d["id"]] = d
            if not frontier:
                break
        versions = sorted(seen.values(), key=lambda d: d.get("dateUploaded") or "")
        latest = [d["id"] for d in versions if not d.get("obsoletedBy")]
        return {"count": len(versions), "latest": latest[-1] if latest else None, "versions": versions}
