"""Parse EML metadata into a compact outline plus structured sections that can be requested on demand.

Large EML documents are mostly repeated structure: a package of daily files often carries the same attribute
list once per file. Entities whose attribute lists are identical (ignoring XML ids) share one ``schema_id``,
so each schema is described once. Values are copied from the document as-is; nothing is summarized.
"""

from __future__ import annotations

import json
from typing import Any

import defusedxml.ElementTree as ET
from fastmcp.exceptions import ToolError

ENTITY_TYPES = ("dataTable", "spatialRaster", "spatialVector", "storedProcedure", "view", "otherEntity")
SECTIONS = ("outline", "methods", "coverage", "people", "project", "entities", "attributes", "annotations", "raw")
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


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, [], {}, "")}


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
        self.entities: list[dict[str, Any]] = []
        self.schemas: dict[str, list[dict[str, Any]]] = {}
        self._group_entities()

    # -- entities and schemas ---------------------------------------------------------------------

    def _resolve(self, el: Any) -> Any:
        """Follow an EML <references> to the element it points at."""
        ref = el.find("references") if el is not None else None
        if ref is not None and ref.text:
            return self._ids.get(ref.text.strip(), el)
        return el

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

    def _attribute(self, el: Any) -> dict[str, Any]:
        el = self._resolve(el)
        out: dict[str, Any] = {
            "name": _text(el, "attributeName"),
            "label": _text(el, "attributeLabel"),
            "definition": _text(el, "attributeDefinition"),
            "storage_type": _text(el, "storageType"),
        }
        ms = el.find("measurementScale")
        scale = next(iter(ms), None) if ms is not None else None
        if scale is not None:
            out["scale"] = _local(scale.tag)
            if (std := _text(scale, "unit/standardUnit")) is not None:
                out["unit"] = std
            elif (custom := _text(scale, "unit/customUnit")) is not None:
                out["unit"] = custom
                out["unit_source"] = "custom"
                out["unit_definition"] = self.custom_units.get(custom)
            out["number_type"] = _text(scale, "numericDomain/numberType")
            bounds = scale.find("numericDomain/bounds")
            if bounds is not None:
                out["bounds"] = _compact({"minimum": _text(bounds, "minimum"), "maximum": _text(bounds, "maximum")})
            out["precision"] = _text(scale, "precision")
            out["date_format"] = _text(scale, "formatString")
            out["date_precision"] = _text(scale, "dateTimePrecision")
            out["text_domain"] = _text(scale, "nonNumericDomain/textDomain/definition")
            out["pattern"] = _texts(scale, "nonNumericDomain/textDomain/pattern")
            out["codes"] = [
                _compact({"code": _text(cd, "code"), "definition": _text(cd, "definition")})
                for cd in scale.findall("nonNumericDomain/enumeratedDomain/codeDefinition")
            ]
        out["missing_values"] = [
            _compact({"code": _text(m, "code"), "explanation": _text(m, "codeExplanation")})
            for m in el.findall("missingValueCode")
        ]
        out["annotations"] = _annotations(el)
        return _compact(out)

    def _group_entities(self) -> None:
        keys: dict[str, str] = {}
        for el in self.dataset:
            kind = _local(el.tag)
            if kind not in ENTITY_TYPES:
                continue
            attr_list = self._resolve(el.find("attributeList"))
            attrs = [self._attribute(a) for a in attr_list.findall("attribute")] if attr_list is not None else []
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
                names = [a.get("name") for a in self.schemas[g["schema_id"]]]
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

    # -- sections ---------------------------------------------------------------------------------

    def methods_text(self) -> str | None:
        parts = []
        for m in self.dataset.findall("methods"):
            for step in m:
                if text := _plain(step):
                    parts.append(text)
        return "\n\n".join(parts) or None

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
                    temporal.append({"date": _plain(d)})
                for r in t.findall("rangeOfDates"):
                    temporal.append(_compact({"begin": _text(r, "beginDate"), "end": _text(r, "endDate")}))
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
        dates = [d for t in cov.get("temporal", []) for d in t.values()]
        if dates:
            out["temporal"] = {"begin": min(dates), "end": max(dates)}
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
            sid: [{"attribute": a.get("name"), "annotations": a["annotations"]} for a in attrs if a.get("annotations")]
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
        rights = d.find("intellectualRights")
        if rights is None:
            rights = d.find("licensed")
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
            "license": _plain(rights),
            "coverage": self.coverage_summary(),
            "methods_preview": methods[:METHODS_PREVIEW_CHARS] + (" ..." if len(methods) > METHODS_PREVIEW_CHARS else "") if methods else None,
            "methods_chars": len(methods) if methods else None,
            "projects": [t for p in d.findall("project") for t in _texts(p, "title")],
            "annotations": _annotations(d),
            "n_entities": len(self.entities),
            "n_schemas": len(self.schemas),
            "entity_groups": groups[:MAX_OUTLINE_GROUPS],
        })
        if len(groups) > MAX_OUTLINE_GROUPS:
            out["entity_groups_omitted"] = len(groups) - MAX_OUTLINE_GROUPS
        return out


def parse(xml_text: str) -> EMLDocument | None:
    """Parse an EML document, or return None if the XML is not EML (e.g. ISO 19115)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ToolError(f"Metadata document is not well-formed XML: {exc}; use section='raw'.") from exc
    if _local(root.tag) != "eml":
        return None
    return EMLDocument(root)


def root_name(xml_text: str) -> str:
    """Local name of the root element, for describing non-EML documents."""
    try:
        return _local(ET.fromstring(xml_text).tag)
    except ET.ParseError:
        return "unparseable"


def page_items(items: list[Any], offset: int, max_chars: int) -> tuple[list[Any], int | None]:
    """Take items from ``offset`` until their JSON size reaches ``max_chars`` (always at least one)."""
    page, used = [], 0
    for item in items[offset:]:
        size = len(json.dumps(item))
        if page and used + size > max_chars:
            break
        page.append(item)
        used += size
    end = offset + len(page)
    return page, end if end < len(items) else None
