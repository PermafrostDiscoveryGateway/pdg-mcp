"""Compare what a dataset's EML says with what its data files contain.

Everything here is a pure function over parsed EML (see :mod:`arcticdata_mcp.eml`) and profile reports (see
:mod:`arcticdata_mcp.profiling`), so it needs no network. Each finding says what the EML claims, what the data show,
how sure we are, and which EML element to change, so an agent can turn a list of findings into proposed edits.

Severity: ``error`` is a contradiction, ``warning`` is a gap or disagreement worth fixing, ``info`` is a suggestion or a
low-confidence difference. Confidence is how much the data side can be trusted: units written in a NetCDF file are
``high``, a units row in a CSV ``medium``, and a unit guessed from a column name ``low``.
"""

from __future__ import annotations

import difflib
import math
import re
from datetime import datetime, timedelta
from typing import Any

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
MAX_FINDINGS = 300
BLANK_CODES = {"", "na", "n/a", "nan", "null", "none", "nodata", "no data", "missing"}

# -- names ---------------------------------------------------------------------------------------------------------


def norm_name(name: str) -> str:
    """A name reduced to lower-case letters and digits, so "Elev.m.", "Elev(m)" and "elev m" compare equal."""
    return re.sub(r"[^0-9a-z]+", "", name.lower()) or name.strip().lower()


def leaf(name: str) -> str:
    """The last component of a NetCDF group path such as ``/group/var``."""
    return name.rsplit("/", 1)[-1]


# -- units ---------------------------------------------------------------------------------------------------------

# Symbol -> the spellings seen for it in EML (standard unit names, singular and plural) and in data files.
BASE_UNITS: dict[str, tuple[str, ...]] = {
    "m": ("meter", "meters", "metre", "metres"),
    "cm": ("centimeter", "centimeters", "centimetre", "centimetres"),
    "mm": ("millimeter", "millimeters", "millimetre", "millimetres"),
    "km": ("kilometer", "kilometers", "kilometre", "kilometres"),
    "m2": ("metersquared", "meterssquared", "squaremeter", "squaremeters"),
    "m3": ("metercubed", "meterscubed", "cubicmeter", "cubicmeters"),
    "cm3": ("cubiccentimeter", "cubiccentimeters", "centimetercubed", "cc"),
    "s": ("sec", "secs", "second", "seconds"),
    "min": ("mins", "minute", "minutes"),
    "h": ("hr", "hrs", "hour", "hours"),
    "d": ("day", "days"),
    "yr": ("year", "years", "yrs"),
    "g": ("gram", "grams"),
    "kg": ("kilogram", "kilograms"),
    "mg": ("milligram", "milligrams"),
    "ug": ("microgram", "micrograms"),
    "l": ("liter", "liters", "litre", "litres"),
    "ml": ("milliliter", "milliliters", "millilitre", "millilitres"),
    "pa": ("pascal", "pascals"),
    "hpa": ("hectopascal", "millibar", "millibars", "mbar", "mb"),
    "kpa": ("kilopascal", "kilopascals"),
    "w": ("watt", "watts"),
    "hz": ("hertz",),
    "v": ("volt", "volts"),
    "mv": ("millivolt", "millivolts"),
    "a": ("ampere", "amperes", "amp", "amps"),
    "us": ("microsiemens",),
    "c": ("celsius", "degc", "degreec", "degreesc", "degreecelsius", "degreescelsius", "oc"),
    "k": ("kelvin",),
    "f": ("fahrenheit", "degf", "degreefahrenheit", "degreesfahrenheit"),
    "deg": ("degs", "wgs84dd", "decimaldegrees", "decimaldegree", "degree", "degrees", "degreenorth", "degreesnorth", "degreeeast", "degreeseast", "degn", "dege"),
    "%": ("pct", "percent"),
    "ppm": ("partspermillion",),
    "ppt": ("permil", "partsperthousand", "\N{PER MILLE SIGN}"),
    "psu": ("pss", "pss-78", "practicalsalinityunit"),
    "count": ("counts", "number", "n", "num"),
    "1": ("unitless", "dimensionless", "none", "n/a", "-"),
}
DIMENSIONLESS_KINDS = {"%", "ppm", "ppt", "count", "psu"}
# Spellings with the slash left out: "m s-1" and "W m-2".
SLASHLESS = {"ms-1": "m/s", "mps": "m/s", "kph": "km/h", "kmh-1": "km/h", "wm-2": "w/m2", "uscm-1": "us/cm"}


def unit_key(unit: str, split: bool = True) -> str:
    """A unit spelling reduced to a comparable form: lower case, no spaces, ``per`` written as ``/``, ASCII digits.

    ``split`` turns EML names such as ``metersPerSecond`` into ``meters/second``; ``partsPerMillion`` is one unit,
    so the unsplit form is tried first.
    """
    u = re.sub(r"(?<=[a-z])Per(?=[A-Z])", "/", unit.strip()) if split else unit.strip()
    u = u.lower()
    for old, new in (("\N{DEGREE SIGN}", "deg"), ("\N{MASCULINE ORDINAL INDICATOR}", "deg"), ("\N{MICRO SIGN}", "u"),
                     ("\N{GREEK SMALL LETTER MU}", "u"), ("\N{SUPERSCRIPT TWO}", "2"), ("\N{SUPERSCRIPT THREE}", "3"),
                     ("\N{SUPERSCRIPT MINUS}\N{SUPERSCRIPT ONE}", "-1")):
        u = u.replace(old, new)
    u = re.sub(r"\s+per\s+", "/", u)
    u = re.sub(r"[\s_^*.]+", "", u)
    return SLASHLESS.get(u, u)


_SYMBOL: dict[str, str] = {}
for _symbol, _spellings in BASE_UNITS.items():
    for _spelling in (_symbol, *_spellings):
        _SYMBOL[unit_key(_spelling)] = _SYMBOL[unit_key(_spelling, split=False)] = _symbol


def canonical_unit(unit: str) -> str | None:
    """A unit as symbols (``g/kg``, ``m/s``, ``c``), or None when a part of it is not in the table."""
    if (whole := _SYMBOL.get(unit_key(unit, split=False))) is not None:
        return whole
    key = unit_key(unit)
    if key.count("/") == 1:
        num, den = (_SYMBOL.get(part) for part in key.split("/"))
        return f"{num}/{den}" if num and den else None
    return _SYMBOL.get(key) if "/" not in key else None


def compare_units(eml_unit: str, data_unit: str) -> str:
    """'same', 'different' (both known and not equivalent) or 'unknown' (not enough knowledge to say)."""
    a, b = canonical_unit(eml_unit), canonical_unit(data_unit)
    if a and b:
        return "same" if a == b else "different"
    return "same" if unit_key(eml_unit) == unit_key(data_unit) else "unknown"


# -- dates ---------------------------------------------------------------------------------------------------------

_DATE_TOKEN = re.compile(r"YYYY|yyyy|YY|yy|MM|mm|DD|dd|hh|HH|ss|SS|S+")


def eml_shape(fmt: str) -> str | None:
    """An EML ``formatString`` such as ``YYYY-MM-DD hh:mm:ss`` as a digit mask (``dddd-dd-dd dd:dd:dd``)."""
    shape = _DATE_TOKEN.sub(lambda m: "d" * len(m.group()), fmt.strip())
    return None if re.search(r"[A-Za-z]", re.sub(r"[dTZ]", "", shape)) else shape


def eml_regex(fmt: str) -> re.Pattern[str] | None:
    """A pattern for an EML ``formatString`` that accepts unpadded numbers: ``MM/DD/YYYY`` matches ``9/4/2019``."""
    if eml_shape(fmt) is None:
        return None
    out, pos = [], 0
    for m in _DATE_TOKEN.finditer(fmt):
        out.append(re.escape(fmt[pos:m.start()]))
        token = m.group()
        out.append(r"\d{4}" if token.upper() == "YYYY" else r"\d{2}" if token.upper() == "YY"
                   else r"\d{1,2}" if len(token) == 2 else r"\d+")
        pos = m.end()
    out.append(re.escape(fmt[pos:]))
    return re.compile("".join(out))


def data_shape(example: str) -> str:
    return re.sub(r"\d", "d", example.strip())


def suggest_format(eml_format: str, example: str) -> str | None:
    """The EML format re-written with the separators the data use, when both have the same digit groups in order."""
    tokens = _DATE_TOKEN.findall(eml_format)
    parts = re.split(r"(d+)", data_shape(example))
    if [len(t) for t in tokens] != [len(p) for p in parts if p.startswith("d")]:
        return None
    it = iter(tokens)
    return "".join(next(it) if p.startswith("d") else p for p in parts)


def parse_when(text: str, end: bool = False) -> datetime | None:
    """An EML or profile date ('2012', '2012-03', '2012-03-17', ISO date-time) as a datetime.

    A coarse value spans a period, so ``end`` selects its last instant: '2012' ends on 2012-12-31.
    """
    m = re.match(r"^\s*(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?(?:[T ](\d{1,2}):(\d{2})(?::(\d{2}))?)?", str(text))
    if not m:
        return None
    year, month, day, hh, mm, ss = m.groups()
    try:
        if hh is not None:
            return datetime(int(year), int(month or 1), int(day or 1), int(hh), int(mm), int(ss or 0))
        if end:
            if month is None:
                return datetime(int(year), 12, 31, 23, 59, 59)
            if day is None:
                nxt = datetime(int(year) + (int(month) == 12), int(month) % 12 + 1, 1)
                return nxt - timedelta(seconds=1)
            return datetime(int(year), int(month), int(day), 23, 59, 59)
        return datetime(int(year), int(month or 1), int(day or 1))
    except ValueError:
        return None


# -- findings ------------------------------------------------------------------------------------------------------


def finding(kind: str, severity: str, confidence: str, message: str, *, schema_id: str | None = None,
            attribute: str | None = None, eml: Any = None, data: Any = None, edit: dict[str, Any] | None = None,
            files: list[str] | None = None, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    out = {"kind": kind, "severity": severity, "confidence": confidence, "message": message}
    for key, value in (("schema_id", schema_id), ("attribute", attribute), ("eml", eml), ("data", data),
                       ("suggested_edit", edit), ("files", files), ("items", items)):
        if value not in (None, [], {}):
            out[key] = value
    return out


def edit(action: str, path: str, value: Any = None) -> dict[str, Any]:
    return {"action": action, "path": path, **({"value": value} if value is not None else {})}


def attr_path(name: str, tail: str = "") -> str:
    return f"attributeList/attribute[attributeName='{name}']" + (f"/{tail}" if tail else "")


# -- facts about data columns --------------------------------------------------------------------------------------


def _dtype_kind(dtype: str | None) -> str | None:
    if not dtype:
        return None
    if dtype.startswith(("int", "uint")):
        return "integer"
    if dtype.startswith("float"):
        return "float"
    return "string"


def facts_from_profile(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """The columns of a table profile, or the variables of a NetCDF profile, in one common shape."""
    out: list[dict[str, Any]] = []
    if profile.get("kind") == "table":
        for c in profile.get("columns", []):
            out.append({
                "name": c["name"], "type": c.get("type"), "min": c.get("min"), "max": c.get("max"),
                "null_count": c.get("null_count", 0), "sentinel_count": c.get("sentinel_count", 0),
                "examples": list(c.get("examples") or []), "datetime_format": c.get("datetime_format"),
                "whole_numbers": bool(c.get("whole_numbers")) or c.get("type") == "integer",
                "top_values": [t["value"] for t in c.get("top_values") or []],
                "unit": c.get("unit_guess"), "unit_confidence": "medium" if c.get("unit_source") == "header row" else "low",
            })
    elif profile.get("kind") == "netcdf":
        for v in profile.get("variables", []):
            if v.get("profiled") is False:
                out.append({"name": v["name"], "type": None, "unprofiled": True})
                continue
            stats = v.get("stats") or {}
            fills = [x for k in ("_FillValue", "missing_value") for x in _as_list(v.get(k))]
            out.append({
                "name": v["name"], "type": _dtype_kind(v.get("dtype")), "min": stats.get("min"), "max": stats.get("max"),
                "unit": v.get("units"), "unit_confidence": "high", "fill_values": fills, "role": v.get("role"),
                "examples": [], "null_count": 0, "sentinel_count": 0,
            })
    return out


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _lower(a: Any, b: Any) -> Any:
    try:
        return a if b is None or (a is not None and a <= b) else b
    except TypeError:
        return a


def _upper(a: Any, b: Any) -> Any:
    try:
        return a if b is None or (a is not None and a >= b) else b
    except TypeError:
        return a


def merge_facts(per_file: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Combine the columns of several files that EML says have the same attributes; keeps first-seen order."""
    merged: dict[str, dict[str, Any]] = {}
    for facts in per_file:
        for f in facts:
            m = merged.setdefault(f["name"], {"name": f["name"], "types": set(), "n_files": 0, "null_count": 0,
                                              "sentinel_count": 0, "examples": [], "fill_values": []})
            m["n_files"] += 1
            m["whole_numbers"] = m.get("whole_numbers", True) and bool(f.get("whole_numbers"))
            if f.get("top_values") and not m.get("top_values"):
                m["top_values"] = f["top_values"]
            if f.get("type"):
                m["types"].add(f["type"])
            if f.get("unprofiled"):
                m["unprofiled"] = True
            m["min"] = _lower(m.get("min"), f.get("min")) if f.get("min") is not None else m.get("min")
            m["max"] = _upper(m.get("max"), f.get("max")) if f.get("max") is not None else m.get("max")
            m["null_count"] += f.get("null_count", 0)
            m["sentinel_count"] += f.get("sentinel_count", 0)
            m["examples"] = (m["examples"] + [e for e in f.get("examples", []) if e not in m["examples"]])[:6]
            m["fill_values"] += [x for x in f.get("fill_values", []) if x not in m["fill_values"]]
            for key in ("datetime_format", "role"):
                if f.get(key) and not m.get(key):
                    m[key] = f[key]
            rank = {None: -1, "low": 0, "medium": 1, "high": 2}
            if f.get("unit") and rank[f["unit_confidence"]] > rank[m.get("unit_confidence")]:
                m["unit"], m["unit_confidence"] = f["unit"], f["unit_confidence"]
    out = []
    for m in merged.values():
        types = m.pop("types")
        m["type"] = (next(iter(types)) if len(types) == 1 else "float" if types == {"integer", "float"}
                     else "mixed" if types else None)
        out.append(m)
    return out


# -- matching ------------------------------------------------------------------------------------------------------


def _alike(a: str, b: str) -> bool:
    return difflib.SequenceMatcher(None, norm_name(a), norm_name(b)).ratio() >= 0.8


def header_unreliable(facts: list[dict[str, Any]]) -> bool:
    """A table whose header was probably not read (many columns named 'Unnamed: N' by pandas)."""
    return sum(f["name"].startswith("Unnamed:") for f in facts) * 3 >= max(len(facts), 1)


def match_columns(attrs: list[dict[str, Any]], facts: list[dict[str, Any]], positional: bool):
    """Pair EML attributes with data columns: exact name, then normalised name, then NetCDF leaf name, then position."""
    pairs: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    used_a: set[int] = set()
    used_f: set[int] = set()

    def run(key, how: str) -> None:
        index: dict[str, list[int]] = {}
        for j, f in enumerate(facts):
            if j not in used_f:
                index.setdefault(key(f["name"]), []).append(j)
        for i, a in enumerate(attrs):
            if i in used_a or not a.get("name"):
                continue
            if js := index.get(key(a["name"])):
                j = js.pop(0)
                pairs.append((a, facts[j], how))
                used_a.add(i)
                used_f.add(j)

    run(lambda n: n, "name")
    run(norm_name, "normalised name")
    run(lambda n: norm_name(leaf(n)), "variable name")
    if positional and len(attrs) == len(facts) and not header_unreliable(facts):
        left = [i for i in range(len(attrs)) if i not in used_a and i not in used_f and attrs[i].get("name")]
        alike = all(_alike(attrs[i]["name"], facts[i]["name"]) for i in left)
        # Position alone is a weak signal: trust it next to some real name matches, or when every name is close.
        if left and (pairs or alike):
            for i in left:
                pairs.append((attrs[i], facts[i], "position"))
                used_a.add(i)
                used_f.add(i)
    return (pairs, [a for i, a in enumerate(attrs) if i not in used_a], [f for j, f in enumerate(facts) if j not in used_f])


# -- one attribute against one column ------------------------------------------------------------------------------


def _g(x: float | None) -> str:
    return "?" if x is None else f"{x:g}"


def _num(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _compare_pair(a: dict[str, Any], f: dict[str, Any], schema_id: str | None, sentinels: list[str],
                  how: str) -> list[dict[str, Any]]:
    name = a["name"]
    out: list[dict[str, Any]] = []

    def add(kind, severity, confidence, message, **kw):
        out.append(finding(kind, severity, confidence, message, schema_id=schema_id, attribute=name, **kw))

    if how == "position":
        close = _alike(name, f["name"])
        add("name_mismatch", "info" if close else "warning", "medium" if close else "low",
            f"Attribute '{name}' lines up by position with data column '{f['name']}'"
            + (", which is spelled almost the same." if close else ", but the names are quite different."),
            eml=name, data=f["name"], edit=edit("set", attr_path(name, "attributeName"), f["name"]))
    elif how == "normalised name" and name != f["name"]:
        add("name_mismatch", "info", "high",
            f"Attribute '{name}' matches column '{f['name']}' apart from case or spacing.",
            eml=name, data=f["name"], edit=edit("set", attr_path(name, "attributeName"), f["name"]))
    if f.get("unprofiled") or not f.get("type") or f["type"] == "empty":
        return out

    scale, dtype = a.get("scale"), f["type"]
    expected = {"ratio": "numeric", "interval": "numeric", "dateTime": "datetime"}.get(scale)
    numeric = dtype in ("integer", "float")
    year_only = bool(a.get("date_format")) and (eml_shape(a["date_format"]) or "") == "dddd"
    if expected == "numeric" and not numeric:
        add("type_mismatch", "warning" if dtype == "mixed" else "error", "medium" if dtype == "mixed" else "high",
            f"'{name}' is described as a numeric {scale} but the data contain "
            f"{'a mix of types across files' if dtype == 'mixed' else dtype + ' values'}.",
            eml={"scale": scale, "number_type": a.get("number_type")},
            data={"type": dtype, "examples": f.get("examples", [])[:3],
                  "non_numeric_values": [v for v in f.get("top_values", []) if _num(v) is None][:5]},
            edit=edit("review", attr_path(name, "measurementScale"), "nominal" if dtype == "string" else None))
    elif (expected == "datetime" and dtype not in ("datetime", "time") and not (numeric and year_only)
          and not (a.get("date_format") and eml_shape(a["date_format"]) is None)):  # e.g. "Month" names: not checkable
        sev = "error" if dtype == "string" else "warning"
        add("type_mismatch", sev, "high" if dtype == "string" else "medium",
            f"'{name}' is described as a dateTime but the data contain {dtype} values.",
            eml={"scale": scale, "date_format": a.get("date_format")}, data={"type": dtype, "examples": f.get("examples", [])[:3]},
            edit=edit("review", attr_path(name, "measurementScale")))
    elif expected == "datetime" and dtype == "time" and "dddd" in (eml_shape(a.get("date_format") or "") or ""):
        add("type_mismatch", "warning", "medium", f"'{name}' has a date format but the data hold only a time of day.",
            eml={"date_format": a["date_format"]}, data={"type": "time", "examples": f.get("examples", [])[:3]})

    if numeric:
        _numeric_checks(a, f, add)
    if dtype == "datetime" and a.get("date_format"):
        _date_format_check(a, f, add)
    _unit_check(a, f, add, expected == "numeric" or (scale is None and numeric))
    _missing_check(a, f, sentinels, add)
    return out


COMMON_MISSING_CODES = {-9, -99, -999, -9999, -99999, -999.9, -99.9, -9.99, -999.99}


def _numeric_checks(a: dict[str, Any], f: dict[str, Any], add) -> None:
    name, lo, hi = a["name"], _num(f.get("min")), _num(f.get("max"))
    ntype = a.get("number_type")
    if ntype in ("integer", "whole", "natural") and f["type"] == "float" and not f.get("whole_numbers"):
        add("number_type_mismatch", "warning", "medium",
            f"'{name}' is declared {ntype} but the data contain non-integer values.",
            eml={"number_type": ntype}, data={"type": "float", "min": lo, "max": hi},
            edit=edit("set", attr_path(name, "measurementScale/*/numericDomain/numberType"), "real"))
    if ntype in ("whole", "natural") and lo is not None and lo < (1 if ntype == "natural" else 0):
        hint = " (a common missing-value code)" if lo in COMMON_MISSING_CODES else ""
        add("outside_declared_bounds", "warning", "high",
            f"'{name}' is declared {ntype} but the data go down to {_g(lo)}{hint}.", eml={"number_type": ntype},
            data={"min": lo, **({"possible_missing_value_code": lo} if hint else {})})
    bounds = a.get("bounds") or {}
    bmin, bmax = _num(bounds.get("minimum")), _num(bounds.get("maximum"))
    below = bmin is not None and lo is not None and lo < bmin
    above = bmax is not None and hi is not None and hi > bmax
    if below or above:
        add("outside_declared_bounds", "warning", "high",
            f"'{name}' has values outside its declared bounds (data {_g(lo)} to {_g(hi)}; EML {bounds.get('minimum', '-')}"
            f" to {bounds.get('maximum', '-')}).",
            eml=bounds, data={"min": lo, "max": hi},
            edit=edit("set", attr_path(name, "measurementScale/*/numericDomain/bounds"),
                      {"minimum": min(lo, bmin) if below else bmin, "maximum": max(hi, bmax) if above else bmax}))


def _date_format_check(a: dict[str, Any], f: dict[str, Any], add) -> None:
    pattern = eml_regex(a["date_format"])
    examples = [e for e in f.get("examples", []) if isinstance(e, str)]
    if pattern is None or not examples:
        return
    if all(pattern.fullmatch(e.strip()) for e in examples):
        return
    suggestion = suggest_format(a["date_format"], examples[0])
    add("date_format_mismatch", "warning", "high",
        f"'{a['name']}' declares date format '{a['date_format']}' but the data look like '{examples[0]}'.",
        eml={"date_format": a["date_format"]}, data={"examples": examples[:3]},
        edit=edit("set", attr_path(a["name"], "measurementScale/dateTime/formatString"), suggestion))


def _unit_check(a: dict[str, Any], f: dict[str, Any], add, numeric_scale: bool) -> None:
    if f.get("role") == "time" or not f.get("unit"):
        return
    name, eml_unit, data_unit, conf = a["name"], a.get("unit"), f["unit"], f["unit_confidence"]
    tail = "measurementScale/*/unit/standardUnit"
    if not eml_unit:
        if numeric_scale:
            add("missing_unit", "warning" if conf != "low" else "info", conf,
                f"'{name}' has no unit in the EML; the data suggest '{data_unit}'.",
                data={"unit": data_unit}, edit=edit("add", attr_path(name, tail), data_unit))
        return
    verdict = compare_units(eml_unit, data_unit)
    if verdict == "same":
        return
    if canonical_unit(eml_unit) == "1" and canonical_unit(data_unit) in DIMENSIONLESS_KINDS:
        add("unit_more_specific", "info", conf,
            f"'{name}' is dimensionless in the EML; the data say '{data_unit}', which is a more specific description.",
            eml={"unit": eml_unit}, data={"unit": data_unit}, edit=edit("review", attr_path(name, tail), data_unit))
    elif verdict == "different":
        if conf == "low":
            return  # a unit guessed from a column name is too weak to contradict the EML
        add("unit_mismatch", "warning", conf, f"'{name}' has unit '{eml_unit}' in the EML but the data suggest '{data_unit}'.",
            eml={"unit": eml_unit}, data={"unit": data_unit}, edit=edit("review", attr_path(name, tail), data_unit))
    elif conf == "high":  # a unit in the file itself that the table cannot yet relate to the EML's
        add("unit_unverified", "info", conf,
            f"'{name}' has unit '{eml_unit}' in the EML and '{data_unit}' in the data; they could not be shown equivalent.",
            eml={"unit": eml_unit}, data={"unit": data_unit})


def _missing_check(a: dict[str, Any], f: dict[str, Any], sentinels: list[str], add) -> None:
    name = a["name"]
    codes = [str(m.get("code", "")).strip() for m in a.get("missing_values", [])]
    code_nums = {x for c in codes if (x := _num(c)) is not None}
    if f.get("sentinel_count"):
        if not code_nums & {x for s in sentinels if (x := _num(s)) is not None}:
            add("undocumented_missing_value", "warning", "medium",
                f"'{name}' has {f['sentinel_count']} cells equal to {' or '.join(sentinels)}, which look like missing-value "
                f"codes, but the EML lists {codes or 'none'}.",
                eml={"missing_value_codes": codes}, data={"sentinel_count": f["sentinel_count"], "sentinels": sentinels},
                edit=edit("add", attr_path(name, "missingValueCode"), {"code": sentinels[0], "codeExplanation": "Missing value"}))
    for fill in f.get("fill_values", []):
        if (x := _num(fill)) is not None and x not in code_nums:
            add("undocumented_missing_value", "warning", "high",
                f"'{name}' has fill value {_g(x)} in the file, which the EML does not list as a missing-value code.",
                eml={"missing_value_codes": codes}, data={"fill_value": fill},
                edit=edit("add", attr_path(name, "missingValueCode"), {"code": _g(x), "codeExplanation": "Fill value"}))
    if f.get("null_count") and not any(c.lower() in BLANK_CODES for c in codes):
        add("blank_values_undocumented", "info", "medium",
            f"'{name}' has {f['null_count']} blank or NA cells and the EML lists no code for them.",
            eml={"missing_value_codes": codes}, data={"blank_count": f["null_count"]},
            edit=edit("add", attr_path(name, "missingValueCode"), {"code": "NA", "codeExplanation": "No data"}))


# -- one group of entities that share an attribute list ------------------------------------------------------------


def compare_group(attrs: list[dict[str, Any]], facts: list[dict[str, Any]], *, kind: str, n_files: int,
                  schema_id: str | None, sentinels: list[str], truncated: bool = False) -> list[dict[str, Any]]:
    """Compare one attribute list with the merged columns of the files profiled for it."""
    pairs, lone_attrs, lone_facts = match_columns(attrs, facts, positional=kind == "table")
    out: list[dict[str, Any]] = []
    for a, f, how in pairs:
        out += _compare_pair(a, f, schema_id, sentinels, how)
    blanks = [x for x in out if x["kind"] == "blank_values_undocumented"]
    if len(blanks) > 1:  # one finding per schema, not one per column
        out = [x for x in out if x["kind"] != "blank_values_undocumented"]
        out.append(finding(
            "blank_values_undocumented", "info", "medium",
            f"{len(blanks)} attributes have blank or NA cells but the EML lists no missing-value code for them: "
            f"{', '.join(b['attribute'] for b in blanks[:6])}{' ...' if len(blanks) > 6 else ''}.",
            schema_id=schema_id, data=[{"attribute": b["attribute"], **b["data"]} for b in blanks],
            edit=edit("add", "attributeList/attribute/missingValueCode", {"code": "NA", "codeExplanation": "No data"})))
    unreliable = kind == "table" and header_unreliable(facts)
    if unreliable:
        out.append(finding(
            "header_unreliable", "info", "high",
            "Many columns have no name in the file, so its header row was probably not read correctly; names were "
            "compared only where they matched.", schema_id=schema_id, data=[f["name"] for f in facts[:8]]))
        lone_attrs, lone_facts = [], []
    elif not pairs and lone_attrs and lone_facts:
        out.append(finding(
            "columns_not_matched", "warning", "low",
            f"None of the {len(lone_attrs)} documented attributes matches any of the {len(lone_facts)} columns by name. "
            "The file may have no header row, or the EML may name the columns differently.",
            schema_id=schema_id, eml=[a.get("name") for a in lone_attrs[:8]], data=[f["name"] for f in lone_facts[:8]]))
        return out
    if lone_attrs and not truncated:
        names = [a["name"] for a in lone_attrs if a.get("name")]
        out.append(finding(
            "documented_column_missing", "error", "high",
            f"{len(names)} documented attribute(s) were not found in the data: {', '.join(names[:8])}"
            f"{' ...' if len(names) > 8 else ''}.",
            schema_id=schema_id, eml=names, edit=edit("review", "attributeList/attribute", "remove or rename")))
    empty = [f for f in lone_facts if f.get("type") == "empty"]
    lone_facts = [f for f in lone_facts if f.get("type") != "empty"]
    if empty:
        out.append(finding(
            "empty_columns", "info", "high",
            f"{len(empty)} column(s) hold no values at all ({', '.join(f['name'] for f in empty[:4])}"
            f"{' ...' if len(empty) > 4 else ''}); often stray trailing delimiters.", schema_id=schema_id,
            data=[f["name"] for f in empty]))
    if lone_facts:
        coordinate = [f for f in lone_facts if f.get("role")]
        others = [f for f in lone_facts if not f.get("role")]
        for group, severity in ((others, "warning"), (coordinate, "info")):
            if group:
                drafts = [draft(f) for f in group]
                out.append(finding(
                    "undocumented_column", severity, "high",
                    f"{len(group)} column(s) in the data are not documented: {', '.join(f['name'] for f in group[:8])}"
                    f"{' ...' if len(group) > 8 else ''}.",
                    schema_id=schema_id, data=drafts, edit=edit("add", "attributeList/attribute", drafts)))
    if n_files > 1:
        seen = [f for f in facts if f.get("n_files", n_files) < n_files and not f["name"].startswith("Unnamed:")]
        if seen:
            out.append(finding(
                "inconsistent_columns", "warning", "high",
                f"{len(seen)} column(s) are missing from some of the {n_files} files checked for this schema: "
                f"{', '.join(f['name'] for f in seen[:8])}.", schema_id=schema_id,
                data=[{"name": f["name"], "files_with_column": f["n_files"]} for f in seen]))
    return _collapse(out, schema_id)


COLLAPSIBLE = {"type_mismatch", "number_type_mismatch", "outside_declared_bounds", "unit_mismatch", "unit_more_specific",
               "unit_unverified", "missing_unit", "undocumented_missing_value", "date_format_mismatch", "name_mismatch"}


def _collapse(findings: list[dict[str, Any]], schema_id: str | None, limit: int = 5) -> list[dict[str, Any]]:
    """Fold many findings of one kind in a schema into a single one with an ``items`` list, to keep output readable."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for f in findings:
        if f["kind"] in COLLAPSIBLE and "attribute" in f:
            groups.setdefault((f["kind"], f["severity"]), []).append(f)
    rank = {"low": 0, "medium": 1, "high": 2}
    for (kind, severity), group in groups.items():
        if len(group) <= limit:
            continue
        findings = [f for f in findings if f not in group]
        names = [f["attribute"] for f in group]
        findings.append(finding(
            kind, severity, min((f["confidence"] for f in group), key=rank.get),
            f"{len(group)} attributes ({', '.join(names[:5])}{' ...' if len(names) > 5 else ''}): {group[0]['message']}",
            schema_id=schema_id,
            items=[{k: f[k] for k in ("attribute", "eml", "data", "suggested_edit") if k in f} for f in group]))
    return findings


def draft(f: dict[str, Any]) -> dict[str, Any]:
    draft = {"name": f["name"], "type": f.get("type")}
    for key in ("unit", "min", "max", "datetime_format"):
        if f.get(key) is not None:
            draft[key] = f[key]
    return draft


# -- coverage ------------------------------------------------------------------------------------------------------


def _boxes(eml_cov: dict[str, Any]) -> list[dict[str, float]]:
    boxes = []
    for g in eml_cov.get("geographic", []):
        try:
            boxes.append({k: float(g[k]) for k in ("west", "east", "north", "south")})
        except (KeyError, ValueError):
            continue
    return boxes


def _in_box(bbox: dict[str, float], box: dict[str, float], tol: float) -> bool:
    if bbox["south"] < box["south"] - tol or bbox["north"] > box["north"] + tol:
        return False
    if box["west"] <= box["east"]:
        return box["west"] - tol <= bbox["west"] and bbox["east"] <= box["east"] + tol
    # The box crosses the antimeridian: its longitudes are west..180 and -180..east.
    return all(lon >= box["west"] - tol or lon <= box["east"] + tol for lon in (bbox["west"], bbox["east"]))


def _data_bbox(extent: dict[str, Any]) -> dict[str, float] | None:
    bbox = extent.get("bbox")
    if not bbox:
        return None
    west, east = bbox["west"], bbox["east"]
    if extent.get("longitude_convention") == "0-360":
        west, east = (x - 360 if x > 180 else x for x in (west, east))
    if west > east:
        return None  # a range that wraps the antimeridian cannot be tested as a plain box
    return {"west": west, "east": east, "north": bbox["north"], "south": bbox["south"]}


def compare_coverage(eml_cov: dict[str, Any], extents: list[dict[str, Any]], tolerance: float) -> list[dict[str, Any]]:
    """Compare the EML's geographic and temporal coverage with the extents found in the data files.

    ``extents`` holds one entry per profiled file: ``{"file", "spatial": {...} | None, "temporal": {...} | None}``.
    """
    out: list[dict[str, Any]] = []
    boxes = _boxes(eml_cov)
    placed = [(e["file"], _data_bbox(e["spatial"])) for e in extents if e.get("spatial")]
    placed = [(name, b) for name, b in placed if b]
    if placed and not boxes:
        union = {"west": min(b["west"] for _, b in placed), "east": max(b["east"] for _, b in placed),
                 "north": max(b["north"] for _, b in placed), "south": min(b["south"] for _, b in placed)}
        out.append(finding("coverage_missing_spatial", "warning", "medium",
                           "The EML has no geographic coverage; the data files contain coordinates.",
                           data=union, edit=edit("add", "coverage/geographicCoverage/boundingCoordinates", union)))
    elif placed:
        outside = [(name, b) for name, b in placed if not any(_in_box(b, box, tolerance) for box in boxes)]
        for box in boxes:
            flipped = {**box, "west": box["east"], "east": box["west"]}
            if box["west"] > box["east"] and (hits := [(n, b) for n, b in outside if _in_box(b, flipped, tolerance)]):
                outside = [x for x in outside if x not in hits]
                out.append(finding(
                    "bounding_box_swapped", "warning", "medium",
                    f"An EML bounding box has west ({box['west']:g}) east of its east ({box['east']:g}), which would "
                    f"cross the antimeridian, but {len(hits)} data file(s) lie inside it when the two are swapped.",
                    eml=box, data=flipped, files=[n for n, _ in hits[:5]],
                    edit=edit("set", "coverage/geographicCoverage/boundingCoordinates",
                              {"west": flipped["west"], "east": flipped["east"]})))
        if outside:
            union = {k: (min if k in ("west", "south") else max)(b[k] for _, b in outside)
                     for k in ("west", "east", "north", "south")}
            out.append(finding(
                "data_outside_spatial_coverage", "warning", "high",
                f"{len(outside)} of {len(placed)} profiled files have coordinates outside every EML bounding box "
                f"(tolerance {tolerance:g} degrees).",
                eml=boxes, data=union, files=[n for n, _ in outside[:5]],
                edit=edit("review", "coverage/geographicCoverage/boundingCoordinates", union)))

    eml_ranges = []
    for t in eml_cov.get("temporal", []):
        begin = parse_when(t.get("begin") or t.get("date") or "")
        end = parse_when(t.get("end") or t.get("date") or "", end=True)
        if begin and end:
            eml_ranges.append((begin, end))
    spans = []
    for e in extents:
        t = e.get("temporal") or {}
        start, end = t.get("start") or "", t.get("end") or ""
        # A year-only value such as "2017" covers all of 2017, so each end of the data range is a period, not a point.
        lows = (parse_when(start), parse_when(start, end=True), parse_when(end), parse_when(end, end=True))
        if all(lows):
            spans.append((e["file"], *lows))
    if spans and not eml_ranges:
        start, end = min(s for _, s, _, _, _ in spans), max(e for _, _, _, _, e in spans)
        out.append(finding("coverage_missing_temporal", "warning", "medium",
                           "The EML has no temporal coverage; the data files contain dates.",
                           data={"begin": start.date().isoformat(), "end": end.date().isoformat()},
                           edit=edit("add", "coverage/temporalCoverage/rangeOfDates",
                                     {"begin": start.date().isoformat(), "end": end.date().isoformat()})))
    elif spans:
        lo, hi = min(b for b, _ in eml_ranges), max(e for _, e in eml_ranges)
        early = [n for n, _, s_hi, _, _ in spans if s_hi < lo - timedelta(days=1)]
        late = [n for n, _, _, e_lo, _ in spans if e_lo > hi + timedelta(days=1)]
        if early or late:
            start, end = min(s for _, s, _, _, _ in spans), max(e for _, _, _, _, e in spans)
            note = (" A date in 1900 is often a spreadsheet's empty-date value." if start.year <= 1900 else "")
            out.append(finding(
                "data_outside_temporal_coverage", "warning", "high",
                f"Dates in the data fall outside the EML temporal coverage ({lo.date()} to {hi.date()}); data span "
                f"{start.date()} to {end.date()}.{note}",
                eml={"begin": lo.date().isoformat(), "end": hi.date().isoformat()},
                data={"begin": start.date().isoformat(), "end": end.date().isoformat()},
                files=list(dict.fromkeys(early + late))[:5],
                edit=edit("review", "coverage/temporalCoverage/rangeOfDates",
                          {"begin": min(lo, start).date().isoformat(), "end": max(hi, end).date().isoformat()})))
    return out


def extent_of(profile: dict[str, Any]) -> dict[str, Any]:
    """The spatial and temporal extents a profile found, whatever kind of file it describes."""
    spatial = profile.get("inferred_spatial_extent") or profile.get("spatial_extent")
    temporal = profile.get("inferred_temporal_extent") or profile.get("temporal_extent")
    return {"file": profile.get("fileName") or profile.get("pid"), "spatial": spatial, "temporal": temporal}


def sort_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f["severity"]], f["kind"]))
