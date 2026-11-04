"""Parse EML metadata into a compact outline plus structured sections that can be requested on demand.

Large EML documents are mostly repeated structure: a package of daily files often carries the same attribute
list once per file. Entities whose attribute lists are identical (ignoring XML ids) share one ``schema_id``,
so each schema is described once. Values are copied from the document as-is; nothing is summarized.
Free-text sections (abstract, methods, ...) are flattened to plain text with headings and lists kept;
structured elements inside them (protocols, citations, data sources, ...) are copied whole next to the text.
"""

from __future__ import annotations

import json
import re
import textwrap
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import defusedxml.ElementTree as ET
from fastmcp.exceptions import ToolError

ENTITY_TYPES = ("dataTable", "spatialRaster", "spatialVector", "storedProcedure", "view", "otherEntity")
# section name -> element below <dataset>
FREE_TEXT_SECTIONS = {
    "abstract": "abstract",
    "methods": "methods",
    "purpose": "purpose",
    "intellectual_rights": "intellectualRights",
    "additional_info": "additionalInfo",
}
SECTIONS = ("outline", *FREE_TEXT_SECTIONS, "coverage", "people", "project", "entities", "attributes", "codes", "annotations", "raw")
# Top-level EML resources other than <dataset>; documents describing these have no outline.
OTHER_RESOURCES = ("citation", "software", "protocol")
# XML attributes that identify an element rather than describe it; they differ in every copy of a schema.
IDENTITY_ATTRS = ("id", "system", "scope")
PEOPLE = {
    "creator": "creators",
    "contact": "contacts",
    "metadataProvider": "metadata_providers",
    "associatedParty": "associated_parties",
    "publisher": "publishers",
}
# Outline limits: enough to choose what to read next. Everything is reachable through a section.
METHODS_PREVIEW_CHARS = 600
MAX_OUTLINE_GROUPS = 25
EXAMPLE_ENTITIES = 3
PREVIEW_ATTRIBUTES = 12
MAX_RAW_DATES = 10
ISO_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2}([T ]\d{2}(:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?)?)?)?$")
# Block-level EML TextType / methods elements that start a new paragraph.
_BLOCK = {"para", "section", "listitem", "description", "methodStep", "sampling", "qualityControl",
          "studyExtent", "samplingDescription"}
# Prose (EML TextType) elements. An element whose children are all prose is rendered as text.
_PROSE = {"para", "section", "itemizedlist", "orderedlist", "listitem", "markdown", "literalLayout",
          "emphasis", "subscript", "superscript", "ulink", "citetitle", "value"}
# Whitespace inside these is meaningful, so only the indentation common to every line is removed.
_VERBATIM = {"markdown", "literalLayout"}
# Structured elements that can sit inside free text. They are copied whole and returned next to the text,
# with a placeholder such as "[protocol 1]" where they appeared. Without child elements they are plain text.
STRUCTURED = {
    "protocol": "protocols",
    "software": "software",
    "instrumentation": "instrumentation",
    "citation": "citations",
    "dataSource": "data_sources",
    "coverage": "sampling_coverage",
    "spatialSamplingUnits": "spatial_sampling_units",
}
_STEP_TITLES = {"sampling": "Sampling", "qualityControl": "Quality control"}
# Marks verbatim text so _clean leaves it alone.
_V_START, _V_END = "\x02", "\x03"


def _local(tag: Any) -> str:
    return tag.split("}")[-1] if isinstance(tag, str) else ""


def _plain(el: Any) -> str | None:
    if el is None:
        return None
    text = " ".join("".join(el.itertext()).split())
    return text or None


def _text(el: Any, path: str) -> str | None:
    return _plain(el.find(path)) if el is not None else None


def _texts(el: Any, path: str) -> list[str]:
    return [t for t in (_plain(x) for x in el.findall(path)) if t]


def _verbatim(el: Any) -> str:
    """Text of a markdown / literalLayout element without blank edge lines or the indentation common to every line."""
    lines = "".join(el.itertext()).expandtabs().split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return textwrap.dedent("\n".join(line.rstrip() for line in lines))


def _mixed(el: Any) -> bool:
    """True if text is interleaved with child elements, so a dict copy would lose the order."""
    return len(el) > 0 and (bool((el.text or "").strip()) or any((c.tail or "").strip() for c in el))


def _clean(text: str) -> str:
    out = []
    for i, seg in enumerate(re.split(f"{_V_START}(.*?){_V_END}", text, flags=re.S)):
        if i % 2:
            out.append(seg)
            continue
        seg = re.sub(r"[ \t\r\f\v]+", " ", seg)
        seg = re.sub(r" *\n *", "\n", seg)
        seg = re.sub(r"(\[citation \d+\]) +(?=[.,;:)])", r"\1", seg)
        out.append(re.sub(r"\n{3,}", "\n\n", seg))
    return "".join(out).strip()


class _FreeTextBuilder:
    """Walks prose into text and copies structured elements whole, leaving a placeholder in the text."""

    def __init__(self, doc: EMLDocument) -> None:
        self.doc = doc
        self.counts: dict[str, int] = {}
        self.elements: dict[str, list[dict[str, Any]]] = {}
        self.anchors: dict[int, str] = {}  # id(item) -> the placeholder that places it in the section text
        self.owner: str | None = None  # placeholder of the top-level copy being built, if any
        self.seen: frozenset[int] = frozenset()  # elements being copied, to stop <references> cycles

    def text(self, el: Any) -> str:
        parts: list[str] = []
        self.walk(el, parts)
        return _clean("".join(parts))

    def walk(self, el: Any, parts: list[str], depth: int = 0, marker: str = "- ") -> None:
        name = _local(el.tag)
        if name in STRUCTURED and len(el):
            ref = self.extract(el)
            parts.append(f" {ref} " if name == "citation" else f"\n\n{ref}\n\n")
            return
        if name in _VERBATIM:
            parts.append(f"\n\n{_V_START}{_verbatim(el)}{_V_END}\n\n")
            return
        if name == "ulink":
            label, url = _plain(el), el.get("url")
            parts.append(f"[{label}]({url})" if label and url and label != url else (url or label or ""))
            return
        if name in _STEP_TITLES:
            parts.append(f"\n\n# {_STEP_TITLES[name]}\n")
        if name == "title":
            parts.append(f"\n\n{'#' * max(1, min(depth, 4))} {_plain(el) or ''}\n")
            return
        if name == "listitem":
            parts.append(f"\n\n{marker}")
        elif (name in _BLOCK or name in STRUCTURED.keys() - {"citation"}) and not re.search(r"\n(-|\d+\.)$", "".join(parts[-3:]).rstrip(" \t\n")):
            # A paragraph directly inside a list item stays on the bullet's line.
            parts.append("\n\n")
        if el.text:
            parts.append(el.text)
        n = 0
        for i, child in enumerate(el):
            kind = _local(child.tag)
            if kind == "value" and ((el.text or "").strip() or i):
                parts.append(" / ")  # the same text in another language
            if kind == "listitem":
                n += 1
                self.walk(child, parts, depth, f"{n}. " if name == "orderedlist" else "- ")
            else:
                self.walk(child, parts, depth + (1 if name == "section" else 0))
            if child.tail:
                parts.append(child.tail)

    def extract(self, el: Any) -> str:
        name = _local(el.tag)
        self.counts[name] = self.counts.get(name, 0) + 1
        ref = f"{name} {self.counts[name]}"
        outer, self.owner = self.owner, self.owner or ref
        try:
            copy = self.tree(el)
        finally:
            self.owner = outer
        item = {"ref": ref, **(copy if isinstance(copy, dict) else {"text": copy})}
        self.elements.setdefault(STRUCTURED[name], []).append(item)
        # An element nested in another copy is placed where that copy's placeholder is.
        self.anchors[id(item)] = f"[{outer or ref}]"
        return f"[{ref}]"

    def tree(self, el: Any) -> Any:
        """Copy an element whole with :meth:`EMLDocument._tree`, rendering prose inside it as text."""
        return self.doc._tree(el, self.seen, prose=self._prose)

    def _prose(self, el: Any, seen: frozenset[int]) -> str:
        # Keep the cycle guard across prose, which can hold further structured elements.
        outer, self.seen = self.seen, seen
        try:
            return self.text(el)
        finally:
            self.seen = outer


@dataclass
class FreeText:
    """A free-text section: prose as text, plus structured elements copied whole, keyed by kind."""

    text: str
    elements: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Where each element's placeholder starts in ``text``; elements without one (licenses) count as 0.
    positions: dict[int, int] = field(default_factory=dict, repr=False)

    def page(self, offset: int, max_chars: int) -> dict[str, Any]:
        """One page of text plus the elements whose placeholders start on that page."""
        page, nxt = page_text(self.text, offset, max_chars)
        end = nxt if nxt is not None else float("inf")
        out: dict[str, Any] = {"chars": len(self.text), "offset": offset, "next_offset": nxt, "text": page}
        for key, items in self.elements.items():
            if on_page := [it for it in items if offset <= self.positions.get(id(it), 0) < end]:
                out[key] = on_page
        return out


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, [], {}, "")}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _node_text(value: Any) -> str | None:
    """Text of a node copied by ``EMLDocument._tree``: a string, or the ``#text`` of a dict."""
    if isinstance(value, dict):
        value = value.get("#text")
    return value if isinstance(value, str) else None


def _size(value: Any) -> int:
    return len(json.dumps(value))


def _date(el: Any) -> str | None:
    """A beginDate/endDate/singleDateTime as ISO text, or its raw text for alternativeTimeScale."""
    if el is None:
        return None
    day, clock = _text(el, "calendarDate"), _text(el, "time")
    if day:
        return f"{day}T{clock}" if clock else day
    return " ".join(t.strip() for t in el.itertext() if t.strip()) or None


def _annotation(el: Any) -> dict[str, Any]:
    prop, val = el.find("propertyURI"), el.find("valueURI")
    return _compact({
        "property": prop.get("label") if prop is not None else None,
        "value": val.get("label") if val is not None else None,
        "property_uri": _plain(prop),
        "value_uri": _plain(val),
    })


def _annotations(el: Any) -> list[dict[str, Any]]:
    return [_annotation(a) for a in el.findall("annotation")]


def _tree_annotation(node: Any) -> dict[str, Any]:
    """The same shape as :func:`_annotation`, from an annotation copied by ``EMLDocument._tree``."""
    def part(v: Any) -> tuple[str | None, str | None]:
        return (v.get("label"), v.get("#text")) if isinstance(v, dict) else (None, v)
    (pl, pu), (vl, vu) = part(node.get("propertyURI")), part(node.get("valueURI"))
    return _compact({"property": pl, "value": vl, "property_uri": pu, "value_uri": vu})


def attribute_name(attr: dict[str, Any]) -> str | None:
    return _node_text(attr.get("attributeName"))


def _code_lists(node: Any) -> list[dict[str, Any]]:
    """Every dict in a copied attribute that holds ``codeDefinition`` entries, in document order."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if "codeDefinition" in node:
            found.append(node)
        for k, v in node.items():
            if k != "codeDefinition":
                found += _code_lists(v)
    elif isinstance(node, list):
        for v in node:
            found += _code_lists(v)
    return found


def attribute_codes(attr: dict[str, Any]) -> list[Any]:
    """All enumerated codes of an attribute, across every enumeratedDomain."""
    return [c for holder in _code_lists(attr) for c in _as_list(holder["codeDefinition"])]


def fit_codes(attr: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Return ``attr`` unchanged if it fits in ``max_chars``; otherwise a copy that keeps only the leading
    enumerated codes that fit, with ``codeDefinition_omitted`` counting the rest (page them with section='codes')."""
    if _size(attr) <= max_chars:
        return attr
    attr = json.loads(json.dumps(attr))
    holders = _code_lists(attr)
    lists = [_as_list(h["codeDefinition"]) for h in holders]
    total = sum(len(codes) for codes in lists)
    if not total:
        return attr
    for h in holders:
        h["codeDefinition"] = []
    budget = max_chars - _size(attr) - len('"codeDefinition_omitted": 1000000, ')
    kept, full = 0, True
    for h, codes in zip(holders, lists):
        if not full:
            break
        for code in codes:
            budget -= _size(code) + 2
            if budget < 0:
                full = False
                break
            h["codeDefinition"].append(code)
            kept += 1
    attr["codeDefinition_omitted"] = total - kept
    return attr


def _person(el: Any) -> dict[str, Any]:
    given = " ".join(_texts(el, "individualName/givenName"))
    name = " ".join(x for x in (_text(el, "individualName/salutation"), given, _text(el, "individualName/surName")) if x)
    address = el.find("address")
    return _compact({
        "name": name or None,
        "organization": _text(el, "organizationName"),
        "position": _text(el, "positionName"),
        "role": _text(el, "role"),
        "email": _texts(el, "electronicMailAddress"),
        "user_id": _texts(el, "userId"),
        "online_url": _texts(el, "onlineUrl"),
        "address": ", ".join(_plain(c) or "" for c in address if _plain(c)) if address is not None else None,
    })


def _display_name(el: Any) -> str | None:
    p = _person(el)
    return p.get("name") or p.get("organization") or p.get("position")


class EMLDocument:
    """One parsed EML document. Build with :func:`parse`."""

    def __init__(self, root: Any) -> None:
        self.root = root
        self.dataset = root.find("dataset")
        if self.dataset is None:
            raise ToolError("EML document has no <dataset> (it may describe software or a protocol); use section='raw'.")
        self._ids = {el.get("id"): el for el in root.iter() if el.get("id")}
        self.custom_units = self._custom_units()
        self.unresolved: set[str] = set()
        self.entities: list[dict[str, Any]] = []
        self.schemas: dict[str, list[dict[str, Any]]] = {}
        self._group_entities()

    # -- entities and schemas ---------------------------------------------------------------------

    def _resolve(self, el: Any) -> tuple[Any, str | None]:
        """Follow an EML <references> to the element it points at. Returns (element, unresolved id)."""
        ref = el.find("references")
        rid = (ref.text or "").strip() if ref is not None else ""
        if not rid:
            return el, None
        target = self._ids.get(rid)
        if target is None:
            self.unresolved.add(rid)
            return el, rid
        return target, None

    def _custom_units(self) -> dict[str, dict[str, Any]]:
        units: dict[str, dict[str, Any]] = {}
        for am in self.root.findall("additionalMetadata"):
            for u in am.iter():
                if _local(u.tag) == "unit" and u.get("id"):
                    desc = next((_plain(c) for c in u if _local(c.tag) == "description"), None)
                    units[u.get("id")] = _compact({
                        "name": u.get("name"), "unit_type": u.get("unitType"), "parent_si": u.get("parentSI"),
                        "multiplier_to_si": u.get("multiplierToSI"), "description": desc,
                    })
        return units

    def _tree(self, el: Any, seen: frozenset[int] = frozenset(),
              prose: Callable[[Any, frozenset[int]], str] | None = None) -> Any:
        """Copy an element and everything under it, shaped like the EML.

        Children become keys (a list when repeated), XML attributes are kept except identity ones, and
        <references> is followed at every level. A custom unit gets its unitList definition attached.
        With ``prose``, an element holding only prose (or text mixed with elements) is rendered by it instead.
        """
        el, missing = self._resolve(el)
        if id(el) in seen:
            return {"reference_cycle": el.get("id")}
        seen = seen | {id(el)}
        if prose is not None:
            kids = [c for c in el if _local(c.tag) and not (missing and _local(c.tag) == "references")]
            if kids and (_mixed(el) or all(_local(c.tag) in _PROSE for c in kids)):
                return prose(el, seen)
        node: dict[str, Any] = {_local(k): v for k, v in el.attrib.items() if _local(k) not in IDENTITY_ATTRS}
        if missing:
            node["unresolved_reference"] = missing
        for child in el:
            tag = _local(child.tag)
            if tag and not (missing and tag == "references"):
                node.setdefault(tag, []).append(self._tree(child, seen, prose))
        node = {k: v[0] if isinstance(v, list) and len(v) == 1 else v for k, v in node.items()}
        text = " ".join(" ".join(p.split()) for p in (el.text, *(c.tail for c in el)) if p and p.strip())
        if _local(el.tag) == "customUnit" and text in self.custom_units:
            node["unit_definition"] = self.custom_units[text]
        if text:
            if not node:
                return text
            node["#text"] = text
        return node

    def _group_entities(self) -> None:
        keys: dict[str, str] = {}
        for el in self.dataset:
            kind = _local(el.tag)
            if kind not in ENTITY_TYPES:
                continue
            attr_list, missing = el.find("attributeList"), None
            if attr_list is not None:
                attr_list, missing = self._resolve(attr_list)
            attrs = [self._tree(a) for a in attr_list.findall("attribute")] if attr_list is not None else []
            schema_id = None
            if attrs:
                key = json.dumps(attrs, sort_keys=True)
                if key not in keys:
                    keys[key] = f"S{len(keys) + 1}"
                    self.schemas[keys[key]] = attrs
                schema_id = keys[key]
            name, file = _text(el, "entityName"), _text(el, "physical/objectName")
            self.entities.append(_compact({
                "index": len(self.entities),
                "type": kind,
                "name": name,
                "id": el.get("id"),
                "description": _text(el, "entityDescription"),
                "file": file if file != name else None,
                "format": _text(el, "physical/dataFormat/externallyDefinedFormat/formatName")
                or ("text" if el.find("physical/dataFormat/textFormat") is not None else None),
                "size": _text(el, "physical/size"),
                "rows": _text(el, "numberOfRecords"),
                "url": _text(el, "physical/distribution/online/url"),
                "schema_id": schema_id,
                "n_attributes": len(attrs),
                "unresolved_reference": missing,
                "annotations": _annotations(el),
            }))

    def entity_groups(self) -> list[dict[str, Any]]:
        groups: dict[Any, dict[str, Any]] = {}
        for e in self.entities:
            # Entities without an attribute list are grouped by type so the outline still counts them.
            key = e.get("schema_id") or ("none", e["type"])
            g = groups.setdefault(key, {"schema_id": e.get("schema_id"), "entity_types": {}, "n_entities": 0,
                                        "n_attributes": e["n_attributes"], "example_entities": []})
            g["entity_types"][e["type"]] = g["entity_types"].get(e["type"], 0) + 1
            g["n_entities"] += 1
            if len(g["example_entities"]) < EXAMPLE_ENTITIES:
                g["example_entities"].append(e.get("name") or e.get("file") or f"#{e['index']}")
        for g in groups.values():
            if g["schema_id"]:
                names = [attribute_name(a) for a in self.schemas[g["schema_id"]]]
                g["attribute_names"] = names[:PREVIEW_ATTRIBUTES] + ([f"... {len(names) - PREVIEW_ATTRIBUTES} more"] if len(names) > PREVIEW_ATTRIBUTES else [])
        return [_compact(g) for g in groups.values()]

    def find_schema(self, schema_id: str | None, entity: str | None) -> str:
        if schema_id:
            if schema_id not in self.schemas:
                raise ToolError(f"Unknown schema_id '{schema_id}'. This document has: {', '.join(self.schemas) or 'none'}.")
            return schema_id
        if entity:
            for e in self.entities:
                if entity in (e.get("name"), e.get("id"), e.get("file"), str(e["index"])):
                    if not e.get("schema_id"):
                        raise ToolError(f"Entity '{entity}' has no attribute list.")
                    return e["schema_id"]
            raise ToolError(f"No entity named '{entity}'. Use section='entities' to list them.")
        if len(self.schemas) == 1:
            return next(iter(self.schemas))
        if not self.schemas:
            raise ToolError("This document defines no attributes.")
        raise ToolError(f"This document has {len(self.schemas)} schemas; pass schema_id ({', '.join(self.schemas)}) or entity.")

    def entities_with_schema(self, schema_id: str) -> list[dict[str, Any]]:
        return [e for e in self.entities if e.get("schema_id") == schema_id]

    def find_attribute(self, schema_id: str, attribute: str | None) -> dict[str, Any]:
        attrs = self.schemas[schema_id]
        if not attribute:
            raise ToolError("section='codes' needs attribute (a name or index from section='attributes').")
        for i, a in enumerate(attrs):
            if attribute in (attribute_name(a), str(i)):
                return a
        raise ToolError(f"Schema {schema_id} has no attribute '{attribute}'. Use section='attributes' to list them.")

    # -- sections ---------------------------------------------------------------------------------

    def free_text(self, section: str) -> FreeText | None:
        """One free-text section, or None if the document lacks it.

        Prose comes back as text. Structured elements inside it (protocols, citations, data sources, ...) are
        copied whole into ``elements`` and marked in the text by a placeholder such as "[protocol 1]".
        ``intellectual_rights`` also returns EML 2.2 <licensed> elements as ``licenses``.
        """
        found = self.dataset.findall(FREE_TEXT_SECTIONS[section])
        licenses = self.dataset.findall("licensed") if section == "intellectual_rights" else []
        if not found and not licenses:
            return None
        builder = _FreeTextBuilder(self)
        text = "\n\n".join(t for t in (builder.text(el) for el in found) if t)
        result = FreeText(text, builder.elements)
        for items in builder.elements.values():
            for item in items:
                result.positions[id(item)] = max(0, text.find(builder.anchors[id(item)]))
        if licenses:
            result.elements["licenses"] = [builder.tree(el) for el in licenses]
        return result

    def methods_text(self) -> str | None:
        methods = self.free_text("methods")
        return methods.text if methods and methods.text else None

    def coverage(self) -> dict[str, Any]:
        geo, temporal, taxa = [], [], []
        for cov in self.dataset.findall("coverage"):
            for g in cov.findall("geographicCoverage"):
                bc = g.find("boundingCoordinates")
                geo.append(_compact({
                    "description": _text(g, "geographicDescription"),
                    "west": _text(bc, "westBoundingCoordinate"), "east": _text(bc, "eastBoundingCoordinate"),
                    "north": _text(bc, "northBoundingCoordinate"), "south": _text(bc, "southBoundingCoordinate"),
                    "altitude_min": _text(bc, "boundingAltitudes/altitudeMinimum"),
                    "altitude_max": _text(bc, "boundingAltitudes/altitudeMaximum"),
                    "altitude_units": _text(bc, "boundingAltitudes/altitudeUnits"),
                }))
            for t in cov.findall("temporalCoverage"):
                for d in t.findall("singleDateTime"):
                    if date := _date(d):
                        temporal.append({"date": date})
                for r in t.findall("rangeOfDates"):
                    if rng := _compact({"begin": _date(r.find("beginDate")), "end": _date(r.find("endDate"))}):
                        temporal.append(rng)
            for tc in cov.iter("taxonomicClassification"):
                taxa.append(_compact({
                    "rank": _text(tc, "taxonRankName"), "value": _text(tc, "taxonRankValue"),
                    "common_names": _texts(tc, "commonName"),
                }))
        return _compact({"geographic": geo, "temporal": temporal, "taxonomic": taxa})

    def coverage_summary(self) -> dict[str, Any]:
        cov = self.coverage()
        geo = cov.get("geographic", [])
        out: dict[str, Any] = {"n_geographic": len(geo)} if geo else {}
        boxes = [g for g in geo if all(k in g for k in ("west", "east", "north", "south"))]
        try:
            nums = [{k: float(g[k]) for k in ("west", "east", "north", "south")} for g in boxes]
        except ValueError:
            nums = []
        # A min/max union is wrong for boxes that cross the antimeridian (west > east), so only one is shown then.
        if len(nums) == 1:
            out["bounding_box"] = nums[0]
        elif nums and all(b["west"] <= b["east"] for b in nums):
            out["bounding_box"] = {
                "west": min(b["west"] for b in nums), "east": max(b["east"] for b in nums),
                "north": max(b["north"] for b in nums), "south": min(b["south"] for b in nums),
            }
        out["places"] = [g["description"] for g in geo[:5] if "description" in g]
        dates = [d for t in cov.get("temporal", []) for d in t.values() if d]
        # Only ISO dates compare correctly as text; anything else (e.g. geologic time scales) is listed as-is.
        iso = sorted((d for d in dates if ISO_DATE.match(d)), key=lambda d: d.replace(" ", "T"))
        other = [d for d in dates if not ISO_DATE.match(d)]
        if dates:
            out["temporal"] = _compact({
                "begin": iso[0] if iso else None, "end": iso[-1] if iso else None,
                "other_values": other[:MAX_RAW_DATES],
                "other_values_omitted": len(other) - MAX_RAW_DATES if len(other) > MAX_RAW_DATES else None,
            })
        if taxa := cov.get("taxonomic"):
            out["n_taxa"] = len(taxa)
        return _compact(out)

    def people(self) -> dict[str, Any]:
        out: dict[str, list[dict[str, Any]]] = {}
        for tag, key in PEOPLE.items():
            if found := [_person(p) for p in self.dataset.findall(tag)]:
                out[key] = found
        return out

    def project(self) -> list[dict[str, Any]]:
        projects = []
        for p in [*self.dataset.findall("project"), *self.dataset.findall("project/relatedProject")]:
            projects.append(_compact({
                "titles": _texts(p, "title"),
                "personnel": [_person(x) for x in p.findall("personnel")],
                "abstract": _text(p, "abstract"),
                "funding": _text(p, "funding"),
                "awards": [_compact({
                    "funder": _text(a, "funderName"), "funder_identifier": _texts(a, "funderIdentifier"),
                    "award_number": _text(a, "awardNumber"), "title": _text(a, "title"), "url": _text(a, "awardUrl"),
                }) for a in p.findall("award")],
                "study_area": _text(p, "studyAreaDescription"),
                "design": _text(p, "designDescription"),
            }))
        return projects

    def annotations(self) -> dict[str, Any]:
        entity_sets: dict[str, dict[str, Any]] = {}
        for e in self.entities:
            if anns := e.get("annotations"):
                s = entity_sets.setdefault(json.dumps(anns, sort_keys=True), {"annotations": anns, "n_entities": 0, "example_entities": []})
                s["n_entities"] += 1
                if len(s["example_entities"]) < EXAMPLE_ENTITIES:
                    s["example_entities"].append(e.get("name"))
        attributes = {
            sid: [
                {"attribute": attribute_name(a), "annotations": [_tree_annotation(x) for x in _as_list(a["annotation"])]}
                for a in attrs if a.get("annotation")
            ]
            for sid, attrs in self.schemas.items()
        }
        # EML 2.2 can also annotate any element by id from a top-level <annotations> block.
        referenced = []
        for block in self.root.findall("annotations"):
            for a in block.findall("annotation"):
                target = self._ids.get(a.get("references", ""))
                referenced.append({"references": a.get("references"), "element": _local(target.tag) if target is not None else None, **_annotation(a)})
        return _compact({
            "dataset": _annotations(self.dataset),
            "entities": list(entity_sets.values()),
            "attributes": {k: v for k, v in attributes.items() if v},
            "referenced": referenced,
        })

    def outline(self) -> dict[str, Any]:
        d = self.dataset
        methods = self.methods_text()
        groups = self.entity_groups()
        rights = _plain(d.find("intellectualRights"))
        if rights is None and (lic := d.find("licensed")) is not None:
            name, url = _text(lic, "licenseName"), _text(lic, "url")
            rights = f"{name} ({url})" if name and url else name or url
        out = _compact({
            "format": "EML",
            "eml_namespace": self.root.tag.split("}")[0].lstrip("{") if "}" in self.root.tag else None,
            "package_id": self.root.get("packageId"),
            "title": " / ".join(_texts(d, "title")) or None,
            "alternate_identifiers": _texts(d, "alternateIdentifier"),
            "pub_date": _text(d, "pubDate"),
            "creators": [n for n in (_display_name(c) for c in d.findall("creator")) if n],
            "keywords": _texts(d, "keywordSet/keyword"),
            "abstract": _text(d, "abstract"),
            "license": rights,
            "coverage": self.coverage_summary(),
            "methods_preview": methods[:METHODS_PREVIEW_CHARS] + (" ..." if len(methods) > METHODS_PREVIEW_CHARS else "") if methods else None,
            "methods_chars": len(methods) if methods else None,
            "projects": [t for p in d.findall("project") for t in _texts(p, "title")],
            "annotations": _annotations(d),
            "n_entities": len(self.entities),
            "n_schemas": len(self.schemas),
            "entity_groups": groups[:MAX_OUTLINE_GROUPS],
            "unresolved_references": sorted(self.unresolved),
        })
        if len(groups) > MAX_OUTLINE_GROUPS:
            out["entity_groups_omitted"] = len(groups) - MAX_OUTLINE_GROUPS
        return out


def parse(xml_text: str) -> tuple[str, EMLDocument | None]:
    """Parse a metadata document into (kind, document). ``document`` is None unless it is an EML dataset;
    ``kind`` then describes it, e.g. 'MI_Metadata' (ISO 19115) or 'eml/software'."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ToolError(f"Metadata document is not well-formed XML: {exc}; use section='raw'.") from exc
    kind = _local(root.tag)
    if kind != "eml":
        return kind, None
    if root.find("dataset") is None:
        other = next((r for r in OTHER_RESOURCES if root.find(r) is not None), "no dataset")
        return f"eml/{other}", None
    return "eml", EMLDocument(root)


class CachedDocument:
    """A fetched metadata document, parsed on first use."""

    def __init__(self, text: str) -> None:
        self.text = text
        self._parsed: tuple[str, EMLDocument | None] | None = None

    def parsed(self) -> tuple[str, EMLDocument | None]:
        if self._parsed is None:
            self._parsed = parse(self.text)
        return self._parsed


class DocumentCache:
    """The last few metadata documents by PID, so paging through one does not refetch and reparse it."""

    def __init__(self, max_entries: int = 4, ttl: float = 600.0) -> None:
        self.max_entries = max_entries
        self.ttl = ttl
        self._entries: OrderedDict[str, tuple[float, CachedDocument]] = OrderedDict()

    async def get(self, pid: str, fetch: Callable[[], Awaitable[str]]) -> CachedDocument:
        now = time.monotonic()
        hit = self._entries.get(pid)
        if hit and now - hit[0] < self.ttl:
            self._entries.move_to_end(pid)
            return hit[1]
        doc = CachedDocument(await fetch())
        self._entries[pid] = (now, doc)
        self._entries.move_to_end(pid)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return doc


def page_text(text: str, offset: int, max_chars: int) -> tuple[str, int | None]:
    """One page of ``text`` and the offset of the next page (None on the last page)."""
    if offset and offset >= len(text):
        raise ToolError(f"offset {offset} is past the end ({len(text)} chars).")
    end = offset + max_chars
    return text[offset:end], end if end < len(text) else None


def page_items(items: list[Any], offset: int, max_chars: int) -> tuple[list[Any], int | None]:
    """Take items from ``offset`` until their JSON size reaches ``max_chars`` (always at least one)."""
    if offset and offset >= len(items):
        raise ToolError(f"offset {offset} is past the end ({len(items)} items).")
    page, used = [], 2  # the list's brackets
    for item in items[offset:]:
        size = _size(item) + (2 if page else 0)  # and the ", " separating it from the previous item
        if page and used + size > max_chars:
            break
        page.append(item)
        used += size
    end = offset + len(page)
    return page, end if end < len(items) else None
