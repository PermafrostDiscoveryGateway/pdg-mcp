"""Helpers for checking that every value in an EML document reaches some section of get_metadata_document.

Two checks use them (issue #29):

- tests/test_eml_schema.py builds one document holding every element and attribute an EML schema defines
  (:func:`schema_document`), each with a unique value, and checks where each value ends up.
- tests/test_live_eml.py does the same for recent documents from the Arctic Data Center.

Paths are written below the root <eml> element: ``dataset/coverage/geographicCoverage`` for an element and
``dataset/@system`` for an XML attribute.
"""

from __future__ import annotations

import itertools
import xml.etree.ElementTree as StdET
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from arcticdata_mcp import eml

SCHEMA_DIR = Path(__file__).parent / "data" / "eml-schema"
XS = "http://www.w3.org/2001/XMLSchema"
XML_NS = "http://www.w3.org/XML/1998/namespace"
# Each version's root namespace; it is also the key in eml.SUPPORTED_NAMESPACES.
ROOT_NAMESPACES = {version: ns for ns, version in eml.SUPPORTED_NAMESPACES.items()}


def _x(tag: str) -> str:
    return f"{{{XS}}}{tag}"


class _Schema:
    """The global declarations of one EML version's .xsd files, keyed by (namespace, name)."""

    KINDS = ("element", "complexType", "simpleType", "group", "attributeGroup", "attribute")

    def __init__(self, directory: Path) -> None:
        self.decls: dict[str, dict[tuple[str, str], tuple[Any, dict[str, str]]]] = {k: {} for k in self.KINDS}
        for path in sorted(directory.glob("*.xsd")):
            nsmap = self._root_namespaces(path)
            root = StdET.parse(path).getroot()
            target = root.get("targetNamespace", "")
            for decl in root:
                kind = decl.tag.removeprefix(f"{{{XS}}}")
                if kind in self.decls and decl.get("name"):
                    self.decls[kind][(target, decl.get("name"))] = (decl, nsmap)

    @staticmethod
    def _root_namespaces(path: Path) -> dict[str, str]:
        """The prefixes declared on the root <xs:schema>; later ones (in documentation) don't apply to QNames."""
        nsmap: dict[str, str] = {}
        for event, item in StdET.iterparse(path, events=("start-ns", "start")):
            if event == "start":
                return nsmap
            prefix, uri = item
            nsmap[prefix] = uri
        return nsmap

    def lookup(self, kind: str, qname: str, nsmap: dict[str, str]) -> tuple[Any, dict[str, str]] | None:
        prefix, _, name = qname.rpartition(":")
        ns = XML_NS if prefix == "xml" else nsmap.get(prefix, "")
        return self.decls[kind].get((ns, name))


class _DocumentBuilder:
    """Builds an XML document with every element and attribute the schema allows, each holding a unique value.

    Every branch of every choice is included, so the result is not valid EML; the parser does not validate,
    and a document with everything in it is what the check needs.

    Each complex type is expanded once, where it first occurs breadth-first, so nearest the root: the
    dataset-level copy of a type is the one filled in, which is also where the parser picks out fields
    rather than copying whole. Every other element of that type gets a single value as its text, so each
    element path still carries a value that must reach some section. (Expanding every occurrence instead
    makes the document grow without useful bound: citations hold coverage, methods hold datasets, and so on.)
    """

    def __init__(self, schema: _Schema) -> None:
        self.schema = schema
        self.counter = itertools.count(1)
        self.expanded: set[int] = set()
        self.queue: deque[tuple[Any, dict[str, str], Any]] = deque()

    def value(self) -> str:
        # "zq12x" cannot occur inside "zq123x" or any other value, so a substring match is exact.
        return f"zq{next(self.counter)}x"

    def build(self, decl: Any, nsmap: dict[str, str], root: Any) -> None:
        self.fill(decl, nsmap, root)
        while self.queue:
            ct, ns, el = self.queue.popleft()
            self.complex(ct, ns, el, ())

    def element(self, decl: Any, nsmap: dict[str, str], parent: Any, stack: tuple[int, ...] = ()) -> None:
        if ref := decl.get("ref"):
            found = self.schema.lookup("element", ref, nsmap)
            if found is None:
                return
            decl, nsmap = found
        el = StdET.SubElement(parent, decl.get("name"))
        self.fill(decl, nsmap, el)

    def fill(self, decl: Any, nsmap: dict[str, str], el: Any) -> None:
        """Fill ``el`` as the element declaration ``decl`` describes, or queue its complex type."""
        found = None
        if type_name := decl.get("type"):
            found = self.schema.lookup("complexType", type_name, nsmap)
        elif (inline := decl.find(_x("complexType"))) is not None:
            found = (inline, nsmap)
        if found is None or id(found[0]) in self.expanded:  # a simple type, or a type filled in elsewhere
            el.text = self.value()
        else:
            self.expanded.add(id(found[0]))
            self.queue.append((*found, el))

    def complex(self, ct: Any, nsmap: dict[str, str], el: Any, stack: tuple[int, ...]) -> None:
        if id(ct) in stack:
            return
        stack = (*stack, id(ct))
        if ct.get("mixed") == "true":
            el.text = self.value()
        self.content(ct, nsmap, el, stack)

    def content(self, node: Any, nsmap: dict[str, str], el: Any, stack: tuple[int, ...]) -> None:
        """Handle the particles, attributes and derivations directly under ``node``."""
        for child in node:
            tag = child.tag.removeprefix(f"{{{XS}}}") if isinstance(child.tag, str) else ""
            if tag == "element":
                self.element(child, nsmap, el, stack)
            elif tag in ("sequence", "choice", "all"):
                self.content(child, nsmap, el, stack)
            elif tag == "group":
                if (found := self.schema.lookup("group", child.get("ref", ""), nsmap)) is not None:
                    self.content(*found, el, stack)
            elif tag == "attribute":
                name = child.get("name") or child.get("ref", "").rpartition(":")[2]
                key = f"{{{XML_NS}}}{name}" if child.get("ref", "").startswith("xml:") else name
                if child.get("use") != "prohibited":
                    el.set(key, self.value())
            elif tag == "attributeGroup":
                if (found := self.schema.lookup("attributeGroup", child.get("ref", ""), nsmap)) is not None:
                    self.content(*found, el, stack)
            elif tag == "complexContent":
                if child.get("mixed") == "true":
                    el.text = self.value()
                self.content(child, nsmap, el, stack)
            elif tag == "simpleContent":
                el.text = self.value()
                self.content(child, nsmap, el, stack)
            elif tag == "extension":
                base = self.schema.lookup("complexType", child.get("base", ""), nsmap)
                if base is not None and id(base[0]) not in stack:
                    self.complex(*base, el, stack)
                self.content(child, nsmap, el, stack)
            elif tag == "restriction":
                # A restriction restates the content it keeps, so only its own particles are used.
                self.content(child, nsmap, el, stack)


def schema_document(version: str) -> str:
    """One EML document of ``version`` holding every element and attribute its schema defines."""
    schema = _Schema(SCHEMA_DIR / version)
    ns = ROOT_NAMESPACES[version]
    decl, nsmap = schema.decls["element"][(ns, "eml")]
    root = StdET.Element(f"{{{ns}}}eml")
    builder = _DocumentBuilder(schema)
    # Not in the EML schema, but every real document has one.
    root.set("{http://www.w3.org/2001/XMLSchema-instance}schemaLocation", builder.value())
    builder.build(decl, nsmap, root)
    return StdET.tostring(root, encoding="unicode")


def matches(pattern: str, path: str, *, subtree: bool = True) -> bool:
    """Whether ``path`` matches ``pattern`` as eml.IGNORED describes: "*" is one element, "**" any number,
    and (with ``subtree``) a pattern also matches everything below it."""

    def match(pat: list[str], parts: list[str]) -> bool:
        if not pat:
            return subtree or not parts
        if pat[0] == "**":
            return any(match(pat[1:], parts[i:]) for i in range(len(parts) + 1))
        return bool(parts) and pat[0] in ("*", parts[0]) and match(pat[1:], parts[1:])

    return match(pattern.split("/"), path.split("/"))


def explained(path: str) -> bool:
    """Whether ``path`` is on eml.IGNORED or eml.KNOWN_GAPS."""
    return any(matches(p, path) for p in (*eml.IGNORED, *eml.KNOWN_GAPS))


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def value_paths(xml_text: str) -> dict[str, set[str]]:
    """Every text and attribute value in a document, grouped by path below the root.

    A run of the same element inside itself (``section/section``) is written once, so nested prose and
    nested structures share one path.
    """
    out: dict[str, set[str]] = {}

    def add(path: str, value: str | None) -> None:
        if value and (value := " ".join(value.split())):
            out.setdefault(path, set()).add(value)

    def walk(el: Any, path: tuple[str, ...]) -> None:
        here = "/".join(path)
        for key, val in el.attrib.items():
            add(f"{here}/@{_local(key)}" if here else f"@{_local(key)}", val)
        add(here, el.text)
        for child in el:
            if not isinstance(child.tag, str):  # comments and processing instructions
                continue
            name = _local(child.tag)
            walk(child, path if path and path[-1] == name else (*path, name))
            add(here, child.tail)

    walk(StdET.fromstring(xml_text), ())
    return out


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _strings(v)
    elif value is not None:
        yield str(value)


def section_text(doc: eml.EMLDocument) -> str:
    """Every string in every section of ``doc``, whitespace collapsed, in one searchable blob."""
    return "\x00".join(" ".join(s.split()) for s in _strings(doc.all_sections()))


def missing_paths(xml_text: str) -> dict[str, list[str]]:
    """Paths with a value that appears in no section's output, with those values (sorted)."""
    kind, doc = eml.parse(xml_text)
    assert doc is not None, f"not an EML dataset: {kind}"
    blob = section_text(doc)
    missing = {path: sorted(v for v in values if v not in blob) for path, values in value_paths(xml_text).items()}
    return {path: values for path, values in sorted(missing.items()) if values}
