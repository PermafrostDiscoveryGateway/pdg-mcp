import httpx
import pytest
from fastmcp.exceptions import ToolError

from arcticdata_mcp import compare
from arcticdata_mcp.tools.compare import interleave, pid_from_url, spread
from conftest import MN
from test_profile import SYSMETA

# Pure comparison logic -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("eml_unit", "data_unit", "verdict"), [
    ("celsius", "degree_Celsius", "same"),
    ("celsius", "deg C", "same"),
    ("metersPerSecond", "m/s", "same"),
    ("meterPerSecond", "m s-1", "same"),
    ("gramPerKilogram", "g/kg", "same"),
    ("wattPerMeterSquared", "W/m\N{SUPERSCRIPT TWO}", "same"),
    ("hectopascal", "mbar", "same"),
    ("degree", "degrees_north", "same"),
    ("meter", "cm", "different"),
    ("millimeter", "mm/h", "different"),
    ("furlong", "fortnight", "unknown"),
    ("furlong", "Furlong", "same"),
    ("partsPerMillion", "ppm", "same"),
    ("gramsPerCubicCentimeter", "g/cm^3", "same"),
    ("degree", "WGS84DD", "same"),
])
def test_compare_units(eml_unit, data_unit, verdict):
    assert compare.compare_units(eml_unit, data_unit) == verdict


def test_date_format_shapes_and_suggestion():
    assert compare.eml_shape("YYYY-MM-DD hh:mm:ss") == "dddd-dd-dd dd:dd:dd"
    assert compare.eml_shape("MMM DD, YYYY") is None  # month names cannot be masked
    assert compare.suggest_format("YYYY-MM-DD hh:mm:ss", "2022-08-16T00:33:49Z") == "YYYY-MM-DDThh:mm:ssZ"
    assert compare.suggest_format("YYYY-MM-DD", "2022-08-16 00:33:49") is None


def test_parse_when_spans_the_period():
    assert compare.parse_when("2022").isoformat() == "2022-01-01T00:00:00"
    assert compare.parse_when("2022", end=True).isoformat() == "2022-12-31T23:59:59"
    assert compare.parse_when("2024-02", end=True).isoformat() == "2024-02-29T23:59:59"
    assert compare.parse_when("not a date") is None


def facts(*cols):
    return [{"null_count": 0, "sentinel_count": 0, "examples": [], "unit_confidence": "medium", **c} for c in cols]


def attrs(*items):
    return [{"scale": "ratio", **a} for a in items]


def kinds(findings):
    return sorted(f["kind"] for f in findings)


def run(a, f, kind="table", **kw):
    return compare.compare_group(a, f, kind=kind, n_files=kw.pop("n_files", 1), schema_id="S1", sentinels=["-9999", "-99999"], **kw)


def test_matching_prefers_exact_then_normalised_then_position():
    a = attrs({"name": "Air Temp"}, {"name": "wind"}, {"name": "x1"})
    f = facts({"name": "air_temp", "type": "float"}, {"name": "wind", "type": "float"}, {"name": "y1", "type": "float"})
    pairs, lone_a, lone_f = compare.match_columns(a, f, positional=True)
    assert [(p[0]["name"], p[1]["name"], p[2]) for p in pairs] == [
        ("wind", "wind", "name"), ("Air Temp", "air_temp", "normalised name"), ("x1", "y1", "position")]
    assert not lone_a and not lone_f
    assert compare.match_columns(a, f, positional=False)[1] == [a[2]]  # no positional fallback outside tables


def test_position_is_only_used_when_the_counts_agree():
    a = attrs({"name": "a"}, {"name": "b"})
    f = facts({"name": "p", "type": "float"}, {"name": "q", "type": "float"}, {"name": "r", "type": "float"})
    assert compare.match_columns(a, f, positional=True)[0] == []


def test_netcdf_group_paths_match_on_the_leaf_name():
    pairs, _, _ = compare.match_columns(attrs({"name": "temp"}), facts({"name": "/obs/temp", "type": "float"}), positional=False)
    assert pairs and pairs[0][2] == "variable name"


def test_documented_and_undocumented_columns():
    found = run(attrs({"name": "a"}, {"name": "ghost"}), facts({"name": "a", "type": "float"}, {"name": "extra", "type": "float", "unit": "m"}), kind="netcdf")
    by = {f["kind"]: f for f in found}
    assert by["documented_column_missing"]["severity"] == "error" and by["documented_column_missing"]["eml"] == ["ghost"]
    assert by["undocumented_column"]["severity"] == "warning"
    assert by["undocumented_column"]["suggested_edit"]["value"] == [{"name": "extra", "type": "float", "unit": "m"}]


def test_a_truncated_profile_does_not_claim_columns_are_missing():
    found = run(attrs({"name": "a"}, {"name": "b"}), facts({"name": "a", "type": "float"}), truncated=True)
    assert "documented_column_missing" not in kinds(found)


def test_coordinate_variables_are_undocumented_but_only_info():
    found = run(attrs({"name": "t"}), facts({"name": "t", "type": "float"}, {"name": "lat", "type": "float", "role": "latitude"}), kind="netcdf")
    assert [(f["kind"], f["severity"]) for f in found] == [("undocumented_column", "info")]


def test_type_mismatches():
    found = run(
        attrs({"name": "n"}, {"name": "when", "scale": "dateTime"}, {"name": "fine"}, {"name": "year", "scale": "dateTime", "date_format": "YYYY"}),
        facts({"name": "n", "type": "string", "examples": ["a"]}, {"name": "when", "type": "float"}, {"name": "fine", "type": "integer"},
              {"name": "year", "type": "integer"}))
    assert [(f["attribute"], f["severity"]) for f in found if f["kind"] == "type_mismatch"] == [("n", "error"), ("when", "warning")]


def test_number_type_and_bounds():
    found = run(
        attrs({"name": "count", "number_type": "integer"}, {"name": "pct", "bounds": {"minimum": "0", "maximum": "100"}},
              {"name": "pos", "number_type": "natural"}),
        facts({"name": "count", "type": "float", "min": 0.5, "max": 3.5}, {"name": "pct", "type": "float", "min": -1.0, "max": 104.0},
              {"name": "pos", "type": "integer", "min": 0, "max": 5}))
    by = {f["attribute"]: f for f in found}
    assert by["count"]["kind"] == "number_type_mismatch"
    assert by["pct"]["kind"] == "outside_declared_bounds" and by["pct"]["suggested_edit"]["value"] == {"minimum": -1.0, "maximum": 104.0}
    assert by["pos"]["kind"] == "outside_declared_bounds"


def test_bounds_inside_the_declared_range_are_fine():
    assert run(attrs({"name": "pct", "bounds": {"minimum": "0", "maximum": "100"}}), facts({"name": "pct", "type": "float", "min": 0.0, "max": 99.0})) == []


def test_unit_findings_follow_the_confidence_of_the_data_side():
    a = attrs({"name": "t", "unit": "celsius"}, {"name": "w", "unit": "meter"}, {"name": "z"}, {"name": "g", "unit": "meter"}, {"name": "u", "unit": "furlong"})
    f = facts({"name": "t", "type": "float", "unit": "degree_Celsius"},
              {"name": "w", "type": "float", "unit": "cm", "unit_confidence": "high"},
              {"name": "z", "type": "float", "unit": "m", "unit_confidence": "low"},
              {"name": "g", "type": "float", "unit": "cm", "unit_confidence": "low"},
              {"name": "u", "type": "float", "unit": "fortnight", "unit_confidence": "high"})
    by = {x["attribute"]: x for x in run(a, f)}
    assert "t" not in by  # celsius == degree_Celsius
    assert (by["w"]["kind"], by["w"]["severity"], by["w"]["confidence"]) == ("unit_mismatch", "warning", "high")
    assert (by["z"]["kind"], by["z"]["severity"]) == ("missing_unit", "info")  # a unit guessed from a name is only a hint
    assert "g" not in by  # and one guessed from a name never contradicts a unit the EML gives
    assert by["u"]["kind"] == "unit_unverified"


def test_netcdf_time_variables_skip_the_unit_check():
    found = run(attrs({"name": "time", "unit": "second"}), facts({"name": "time", "type": "float", "unit": "seconds since 1970-01-01", "role": "time", "unit_confidence": "high"}), kind="netcdf")
    assert found == []


def test_sentinels_and_fill_values_must_be_listed_as_missing_value_codes():
    codes = [{"code": "-9999"}]
    a = attrs({"name": "a", "missing_values": codes}, {"name": "b"}, {"name": "c", "missing_values": [{"code": "-999"}]}, {"name": "d", "missing_values": codes})
    f = facts({"name": "a", "type": "float", "sentinel_count": 4}, {"name": "b", "type": "float", "sentinel_count": 2},
              {"name": "c", "type": "float", "sentinel_count": 1}, {"name": "d", "type": "float", "fill_values": [-1.0e30]})
    found = {x["attribute"]: x for x in run(a, f)}
    assert "a" not in found
    assert found["b"]["kind"] == found["c"]["kind"] == "undocumented_missing_value"
    assert found["d"]["data"] == {"fill_value": -1.0e30} and found["d"]["confidence"] == "high"


def test_a_common_missing_code_below_a_natural_range_is_called_out():
    found = run(attrs({"name": "stems", "number_type": "natural"}), facts({"name": "stems", "type": "integer", "min": -999, "max": 40}))
    assert found[0]["kind"] == "outside_declared_bounds" and "missing-value code" in found[0]["message"]


def test_columns_that_disagree_on_type_between_files_are_a_warning():
    merged = compare.merge_facts([facts({"name": "h", "type": "float"}), facts({"name": "h", "type": "string"})])
    found = run(attrs({"name": "h"}), merged, n_files=2)
    assert [(f["kind"], f["severity"]) for f in found] == [("type_mismatch", "warning")]


def test_blank_cells_are_info_unless_a_blank_code_is_listed():
    a = attrs({"name": "a"}, {"name": "b", "missing_values": [{"code": "NaN"}]})
    f = facts({"name": "a", "type": "float", "null_count": 7}, {"name": "b", "type": "float", "null_count": 7})
    found = run(a, f)
    assert [(x["attribute"], x["kind"], x["severity"]) for x in found] == [("a", "blank_values_undocumented", "info")]


def test_unpadded_dates_match_a_padded_format_and_other_shapes_do_not():
    a = attrs(*({"name": n, "scale": "dateTime", "date_format": f} for n, f in
                (("us", "MM/DD/YYYY"), ("us2", "MM/DD/YY hh:mm"), ("iso", "YYYY-MM-DD"), ("name", "Month"))))
    f = facts({"name": "us", "type": "datetime", "examples": ["9/24/2019", "10/5/2019"]},
              {"name": "us2", "type": "datetime", "examples": ["4/29/24 16:00", "4/30/24 0:00"]},
              {"name": "iso", "type": "datetime", "examples": ["7/27/18"]},
              {"name": "name", "type": "string", "examples": ["June"]})
    assert [(x["kind"], x["attribute"]) for x in run(a, f)] == [("date_format_mismatch", "iso")]  # "Month" is not checkable


def test_date_format_mismatch_suggests_the_formats_the_data_use():
    a = attrs({"name": "time", "scale": "dateTime", "date_format": "YYYY-MM-DD hh:mm:ss"}, {"name": "ok", "scale": "dateTime", "date_format": "YYYY-MM-DD"})
    f = facts({"name": "time", "type": "datetime", "examples": ["2022-08-16T00:33:49Z"]}, {"name": "ok", "type": "datetime", "examples": ["2022-08-16"]})
    found = run(a, f)
    assert [(x["kind"], x["attribute"]) for x in found] == [("date_format_mismatch", "time")]
    assert found[0]["suggested_edit"]["value"] == "YYYY-MM-DDThh:mm:ssZ"


def test_merged_facts_cover_every_file_and_flag_columns_missing_from_some():
    one = facts({"name": "a", "type": "integer", "min": 1, "max": 5, "null_count": 1}, {"name": "b", "type": "float", "min": 0.0, "max": 1.0})
    two = facts({"name": "a", "type": "float", "min": -2, "max": 3, "null_count": 2})
    merged = {m["name"]: m for m in compare.merge_facts([one, two])}
    assert (merged["a"]["type"], merged["a"]["min"], merged["a"]["max"], merged["a"]["null_count"], merged["a"]["n_files"]) == ("float", -2, 5, 3, 2)
    found = run(attrs({"name": "a"}, {"name": "b"}), list(merged.values()), n_files=2)
    assert "inconsistent_columns" in kinds(found)


def test_merged_facts_keep_the_most_trusted_unit():
    merged = compare.merge_facts([facts({"name": "a", "type": "float", "unit": "guess", "unit_confidence": "low"}),
                                  facts({"name": "a", "type": "float", "unit": "header", "unit_confidence": "medium"})])
    assert merged[0]["unit"] == "header"


def test_facts_from_a_netcdf_profile():
    profile = {"kind": "netcdf", "variables": [
        {"name": "t", "dtype": "float32", "units": "K", "_FillValue": -999.0, "stats": {"min": 1.0, "max": 2.0}},
        {"name": "lat", "dtype": "float64", "role": "latitude", "units": "degrees_north"},
        {"name": "later", "profiled": False}]}
    f = {x["name"]: x for x in compare.facts_from_profile(profile)}
    assert (f["t"]["type"], f["t"]["unit"], f["t"]["unit_confidence"], f["t"]["fill_values"]) == ("float", "K", "high", [-999.0])
    assert f["lat"]["role"] == "latitude" and f["later"]["unprofiled"]


def test_dimensionless_in_the_eml_with_percent_in_the_data_is_only_a_refinement():
    found = run(attrs({"name": "cover", "unit": "dimensionless"}), facts({"name": "cover", "type": "float", "unit": "Percent"}))
    assert [(f["kind"], f["severity"]) for f in found] == [("unit_more_specific", "info")]


def test_names_that_differ_only_by_punctuation_match_without_needing_position():
    pairs, _, _ = compare.match_columns(attrs({"name": "Elev.m."}, {"name": '"ts"'}), facts({"name": "Elev(m)", "type": "float"}, {"name": "ts", "type": "float"}), positional=False)
    assert [p[2] for p in pairs] == ["normalised name", "normalised name"]


def test_positional_name_mismatches_are_graded_by_how_alike_the_names_are():
    found = run(attrs({"name": "id"}, {"name": "Soil Moisture (from Komarkova 1983)"}, {"name": "alpha"}),
                facts({"name": "id", "type": "float"}, {"name": "Soil Moisture (from Kom\x87rkov\x87 1983)", "type": "float"},
                      {"name": "zzzz", "type": "float"}))
    got = {f["attribute"]: (f["severity"], f["confidence"]) for f in found if f["kind"] == "name_mismatch"}
    assert got == {"Soil Moisture (from Komarkova 1983)": ("info", "medium"), "alpha": ("warning", "low")}


def test_floats_that_are_all_whole_numbers_do_not_break_an_integer_declaration():
    a = attrs({"name": "depth", "number_type": "whole"}, {"name": "lat", "number_type": "integer"})
    f = facts({"name": "depth", "type": "float", "whole_numbers": True}, {"name": "lat", "type": "float", "min": 56.6, "max": 71.8})
    assert [x["attribute"] for x in run(a, f) if x["kind"] == "number_type_mismatch"] == ["lat"]


def test_merging_keeps_whole_numbers_only_if_every_file_agrees():
    merged = compare.merge_facts([facts({"name": "d", "type": "float", "whole_numbers": True}), facts({"name": "d", "type": "float", "whole_numbers": False})])
    assert merged[0]["whole_numbers"] is False


def test_a_numeric_column_that_is_text_names_the_text_values():
    found = run(attrs({"name": "thick"}), facts({"name": "thick", "type": "string", "examples": ["34.00"], "top_values": ["34.00", "n/a", "tbd"]}))
    assert found[0]["data"]["non_numeric_values"] == ["n/a", "tbd"]


def test_blank_cells_are_reported_once_per_schema():
    a = attrs({"name": "a"}, {"name": "b"}, {"name": "c"})
    found = run(a, facts(*({"name": n, "type": "float", "null_count": 3} for n in "abc")))
    assert [f["kind"] for f in found] == ["blank_values_undocumented"] and len(found[0]["data"]) == 3


def test_empty_columns_are_not_called_undocumented():
    cols = [{"name": n, "type": "float"} for n in "abcdefg"]
    found = run(attrs(*({"name": n} for n in "abcdefg")), facts(*cols, {"name": "Unnamed: 7", "type": "empty"}, {"name": "Unnamed: 8", "type": "empty"}))
    assert [(f["kind"], f["severity"]) for f in found] == [("empty_columns", "info")]


def test_position_alone_is_not_trusted_when_no_names_match():
    found = run(attrs({"name": "Borehole Location"}, {"name": "Tare"}), facts({"name": "Dataset Title: Site A", "type": "string"}, {"name": "21-13", "type": "float"}))
    assert [(f["kind"], f["severity"]) for f in found] == [("columns_not_matched", "warning")]


def test_position_is_trusted_when_every_name_is_close():
    found = run(attrs({"name": "Elev.m."}, {"name": "Temp (C)"}), facts({"name": "Elev(m)", "type": "float"}, {"name": "Temp (C) ", "type": "float"}))
    assert not any(f["kind"] in ("columns_not_matched", "documented_column_missing") for f in found)


def test_an_unread_header_stops_column_names_being_compared():
    cols = [{"name": "a", "type": "float"}] + [{"name": f"Unnamed: {i}", "type": "float"} for i in range(1, 5)]
    found = run(attrs(*({"name": n} for n in ("a", "b", "c", "d", "e"))), facts(*cols))
    assert [f["kind"] for f in found] == ["header_unreliable"]


def test_many_findings_of_one_kind_fold_into_one_with_items():
    names = [f"c{i}" for i in range(8)]
    found = run(attrs(*({"name": n, "number_type": "integer"} for n in names)), facts(*({"name": n, "type": "float", "min": 0.5, "max": 1.5} for n in names)))
    assert [f["kind"] for f in found] == ["number_type_mismatch"] and len(found[0]["items"]) == 8
    assert found[0]["items"][0]["attribute"] == "c0" and found[0]["message"].startswith("8 attributes")


# Coverage ----------------------------------------------------------------------------------------------------------


def box(w, e, n, s):
    return {"geographic": [{"west": str(w), "east": str(e), "north": str(n), "south": str(s)}]}


def spatial(w, e, n, s, **extra):
    return {"file": "f.csv", "spatial": {"bbox": {"west": w, "east": e, "north": n, "south": s}, **extra}}


def test_data_inside_the_eml_box_is_fine_and_near_misses_are_tolerated():
    assert compare.compare_coverage(box(-47, -46, 67, 66), [spatial(-46.5, -46.5, 66.5, 66.5)], 0.05) == []
    assert compare.compare_coverage(box(-47, -46, 67, 66), [spatial(-46.5, -46.5, 67.04, 66.5)], 0.05) == []


def test_a_point_that_is_not_where_the_eml_says_is_reported():
    found = compare.compare_coverage(box(-46.3, -46.3, 66.48, 66.48), [spatial(-46.18, -46.18, 66.29, 66.29)], 0.05)
    assert [f["kind"] for f in found] == ["data_outside_spatial_coverage"]
    assert found[0]["data"]["north"] == 66.29 and found[0]["files"] == ["f.csv"]


def test_zero_to_360_longitudes_are_converted_before_comparing():
    assert compare.compare_coverage(box(-170, -160, 70, 60), [spatial(190, 200, 65, 62, longitude_convention="0-360")], 0.05) == []


def test_eml_boxes_across_the_antimeridian():
    cov = box(170, -170, 70, 60)
    assert compare.compare_coverage(cov, [spatial(175, 179, 65, 62)], 0.05) == []
    assert compare.compare_coverage(cov, [spatial(-175, -171, 65, 62)], 0.05) == []
    assert compare.compare_coverage(cov, [spatial(0, 10, 65, 62)], 0.05)


def test_a_file_must_fit_inside_one_box_not_just_the_union():
    cov = {"geographic": [{"west": "0", "east": "1", "north": "1", "south": "0"}, {"west": "10", "east": "11", "north": "11", "south": "10"}]}
    assert compare.compare_coverage(cov, [spatial(0.2, 0.4, 0.5, 0.2)], 0.05) == []
    assert compare.compare_coverage(cov, [spatial(0.2, 10.5, 10.5, 0.2)], 0.05)


def test_a_box_with_west_and_east_swapped_is_named_as_such():
    cov = box(-111.55, -168.58, 76.2, 64.7)  # west is east of east: read as crossing the antimeridian
    found = compare.compare_coverage(cov, [spatial(-168.5, -111.6, 76.0, 65.0)], 0.05)
    assert [f["kind"] for f in found] == ["bounding_box_swapped"]
    assert found[0]["suggested_edit"]["value"] == {"west": -168.58, "east": -111.55}


def test_missing_eml_coverage_is_filled_from_the_data():
    found = compare.compare_coverage({}, [spatial(-150, -149, 61, 60), {"file": "g", "temporal": {"start": "2020-01-02T00:00:00", "end": "2021-05-01T00:00:00"}}], 0.05)
    assert sorted(f["kind"] for f in found) == ["coverage_missing_spatial", "coverage_missing_temporal"]
    assert next(f for f in found if f["kind"] == "coverage_missing_temporal")["data"] == {"begin": "2020-01-02", "end": "2021-05-01"}


def test_temporal_coverage_uses_the_whole_period_of_a_coarse_eml_date():
    cov = {"temporal": [{"begin": "2022", "end": "2024"}]}
    assert compare.compare_coverage(cov, [{"file": "f", "temporal": {"start": "2022-08-16T00:33:49", "end": "2023-12-31T00:00:00"}}], 0.05) == []
    found = compare.compare_coverage(cov, [{"file": "f", "temporal": {"start": "2021-12-01T00:00:00", "end": "2025-01-05T00:00:00"}}], 0.05)
    assert [f["kind"] for f in found] == ["data_outside_temporal_coverage"] and found[0]["data"]["end"] == "2025-01-05"


def test_a_year_only_data_range_is_a_period_not_the_first_of_january():
    cov = {"temporal": [{"begin": "2017-07-13", "end": "2023-09-04"}]}
    inside = {"file": "f", "temporal": {"start": "2017", "end": "2022"}}
    assert compare.compare_coverage(cov, [inside], 0.05) == []
    before = {"file": "f", "temporal": {"start": "2016", "end": "2022"}}
    assert [f["kind"] for f in compare.compare_coverage(cov, [before], 0.05)] == ["data_outside_temporal_coverage"]


def test_a_1900_start_date_is_explained():
    cov = {"temporal": [{"begin": "2005", "end": "2007"}]}
    found = compare.compare_coverage(cov, [{"file": "f", "temporal": {"start": "1900-01-01T00:00:00", "end": "2007-01-01T00:00:00"}}], 0.05)
    assert "spreadsheet" in found[0]["message"]


def test_findings_sort_by_severity():
    got = compare.sort_findings([compare.finding("b", "info", "low", "m"), compare.finding("a", "error", "high", "m"), compare.finding("c", "warning", "high", "m")])
    assert [f["severity"] for f in got] == ["error", "warning", "info"]


# Helpers -----------------------------------------------------------------------------------------------------------


def test_pid_from_url():
    assert pid_from_url("https://cn.dataone.org/cn/v2/resolve/urn%3Auuid%3Aabc") == "urn:uuid:abc"
    assert pid_from_url("https://cn.dataone.org/cn/v2/resolve/urn:uuid:abc") == "urn:uuid:abc"
    assert pid_from_url("https://arcticdata.io/metacat/d1/mn/v2/object/doi%3A10.18739%2FX?a=1") == "doi:10.18739/X"
    assert pid_from_url("ecogrid://knb/file.csv") is None and pid_from_url(None) is None


def test_spread_and_interleave():
    picked = spread(list(range(10)), 3)
    assert len(picked) == 3 and picked[0] == 0 and picked[-1] == 9
    assert spread([1, 2], 5) == [1, 2] and spread([1, 2, 3], 1) == [1]
    assert interleave([[1, 2, 3], [10], [20, 21]]) == [1, 10, 20, 2, 21, 3]


# Tool --------------------------------------------------------------------------------------------------------------

META = "doi:10.18739/CMP"


def entity(n, pid, attrs_xml, name="obs.csv"):
    link = f"https://cn.dataone.org/cn/v2/resolve/{pid.replace(':', '%3A')}"
    return f"""<dataTable><entityName>site{n}/{name}</entityName><physical><objectName>{name}</objectName>
      <distribution><online><url function="download">{link}</url></online></distribution></physical>
      <attributeList>{attrs_xml}</attributeList></dataTable>"""


ATTRS = """
<attribute><attributeName>time</attributeName><measurementScale><dateTime><formatString>YYYY-MM-DD hh:mm:ss</formatString></dateTime></measurementScale></attribute>
<attribute><attributeName>temp</attributeName><measurementScale><ratio><unit><standardUnit>celsius</standardUnit></unit>
  <numericDomain><numberType>real</numberType></numericDomain></ratio></measurementScale></attribute>
<attribute><attributeName>ghost</attributeName><measurementScale><ratio><unit><standardUnit>meter</standardUnit></unit>
  <numericDomain><numberType>real</numberType></numericDomain></ratio></measurementScale></attribute>"""


def eml_text(entities):
    return f"""<?xml version="1.0"?><eml:eml xmlns:eml="https://eml.ecoinformatics.org/eml-2.2.0" packageId="{META}" system="knb"><dataset>
      <title>Compare me</title>
      <coverage><temporalCoverage><rangeOfDates><beginDate><calendarDate>2022-01-01</calendarDate></beginDate>
        <endDate><calendarDate>2022-12-31</calendarDate></endDate></rangeOfDates></temporalCoverage></coverage>
      {entities}</dataset></eml:eml>"""


def csv_body(day):
    return f"time,temp\nUTC,degree_Celsius\n2022-0{day}-01T00:00:00Z,1.5\n2022-0{day}-02T00:00:00Z,2.5\n"


def mock_dataset(mock_api, n_files=3, schema=ATTRS, obsoleted_by=None, current_exists=True):
    pids = [f"urn:uuid:file{i}" for i in range(n_files)]
    xml = eml_text("".join(entity(i, p, schema) for i, p in enumerate(pids)))
    mock_api.get(f"{MN}/object/doi%3A10.18739%2FCMP").mock(return_value=httpx.Response(200, text=xml))

    def solr(request):
        q = request.url.params["q"]
        extra = {"obsoletedBy": obsoleted_by} if obsoleted_by else {}
        if q == f'id:"{META}"':
            docs = [{"id": META, "formatType": "METADATA", "resourceMap": ["resource_map_x"], **extra}]
        elif q == 'id:"urn:uuid:file1"':
            docs = [{"id": "urn:uuid:file1", "formatType": "DATA", "isDocumentedBy": ["doi:10.18739/OLD", META]}]
        elif q.startswith("id:"):  # the metadata record for a data file; a superseded one only turns up without the filter
            superseded_filter = "-obsoletedBy:*" in request.url.params.get_list("fq")
            docs = [] if superseded_filter and not current_exists else [{"id": META, "resourceMap": ["resource_map_x"], **extra}]
        else:
            docs = []
        return httpx.Response(200, json={"response": {"numFound": len(docs), "docs": docs}})

    mock_api.get(f"{MN}/query/solr/").mock(side_effect=solr)
    for i, p in enumerate(pids):
        quoted = p.replace(":", "%3A")
        body = csv_body(i + 1).encode()
        mock_api.get(f"{MN}/meta/{quoted}").mock(return_value=httpx.Response(
            200, text=SYSMETA.format(pid=p, fmt="text/csv", size=len(body), name="obs.csv")))
        mock_api.get(f"{MN}/object/{quoted}").mock(return_value=httpx.Response(200, content=body))
    return pids


async def test_compare_reports_mismatches_for_a_dataset(client, mock_api):
    mock_dataset(mock_api)
    sc = (await client.call_tool("compare_eml_vs_data", {"pid": META})).structured_content
    assert sc["title"] == "Compare me" and sc["summary"]["files_profiled"] == 3 and sc["summary"]["entities"] == 3
    assert sc["groups"][0]["schema_id"] == "S1" and len(sc["groups"][0]["files"]) == 3
    kinds_found = {f["kind"] for f in sc["findings"]}
    assert {"documented_column_missing", "date_format_mismatch"} <= kinds_found
    assert "unit_mismatch" not in kinds_found  # celsius == degree_Celsius
    assert sc["findings"][0]["severity"] == "error" and sc["summary"]["findings"]["error"] == 1
    missing = next(f for f in sc["findings"] if f["kind"] == "documented_column_missing")
    assert missing["eml"] == ["ghost"] and missing["schema_id"] == "S1"
    fmt = next(f for f in sc["findings"] if f["kind"] == "date_format_mismatch")
    assert fmt["suggested_edit"]["value"] == "YYYY-MM-DDThh:mm:ssZ"


async def test_compare_profiles_a_few_files_per_schema_unless_asked_for_all(client, mock_api):
    mock_dataset(mock_api, n_files=5)
    two = (await client.call_tool("compare_eml_vs_data", {"pid": META, "files_per_schema": 2})).structured_content
    assert two["summary"]["files_profiled"] == 2 and [f["pid"] for f in two["groups"][0]["files"]] == ["urn:uuid:file0", "urn:uuid:file4"]
    everything = (await client.call_tool("compare_eml_vs_data", {"pid": META, "files_per_schema": "all"})).structured_content
    assert everything["summary"]["files_profiled"] == 5


async def test_compare_caps_the_total_number_of_files(client, mock_api, settings):
    settings.compare.max_files = 2
    mock_dataset(mock_api, n_files=5)
    sc = (await client.call_tool("compare_eml_vs_data", {"pid": META, "files_per_schema": "all"})).structured_content
    assert sc["summary"]["files_profiled"] == 2 and any("at most 2" in n for n in sc["notes"])


async def test_compare_stops_starting_files_when_the_budget_is_gone(client, mock_api, settings):
    settings.compare.budget = -1
    mock_dataset(mock_api)
    sc = (await client.call_tool("compare_eml_vs_data", {"pid": META})).structured_content
    assert sc["summary"]["files_profiled"] == 0 and sc["summary"]["files_skipped"] == 3 and any("budget" in n for n in sc["notes"])


async def test_compare_with_a_data_file_pid_checks_only_that_file(client, mock_api):
    mock_dataset(mock_api)
    sc = (await client.call_tool("compare_eml_vs_data", {"pid": "urn:uuid:file1"})).structured_content
    assert sc["pid"] == META and sc["summary"]["files_profiled"] == 1
    assert [f["pid"] for f in sc["groups"][0]["files"]] == ["urn:uuid:file1"]


async def test_compare_records_files_that_cannot_be_read(client, mock_api):
    pids = mock_dataset(mock_api, n_files=2)
    mock_api.get(f"{MN}/meta/{pids[1].replace(':', '%3A')}").mock(return_value=httpx.Response(404))
    sc = (await client.call_tool("compare_eml_vs_data", {"pid": META, "files_per_schema": "all"})).structured_content
    assert sc["summary"]["files_profiled"] == 1 and sc["summary"]["files_failed"] == 1
    assert any(f["status"] == "error" for f in sc["groups"][0]["files"])


async def test_compare_reports_entities_that_have_no_attribute_list(client, mock_api):
    mock_dataset(mock_api, n_files=1, schema="")
    sc = (await client.call_tool("compare_eml_vs_data", {"pid": META})).structured_content
    finding = sc["findings"][0]
    assert finding["kind"] == "no_attribute_list" and [d["name"] for d in finding["data"]] == ["time", "temp"]


async def test_compare_notes_when_the_record_has_been_superseded(client, mock_api):
    mock_dataset(mock_api, obsoleted_by="doi:10.18739/NEWER")
    sc = (await client.call_tool("compare_eml_vs_data", {"pid": META})).structured_content
    assert any("superseded by 'doi:10.18739/NEWER'" in n for n in sc["notes"]) and sc["summary"]["files_profiled"] == 3


async def test_compare_falls_back_to_a_superseded_record_for_a_data_file(client, mock_api):
    mock_dataset(mock_api, obsoleted_by="doi:10.18739/NEWER", current_exists=False)
    sc = (await client.call_tool("compare_eml_vs_data", {"pid": "urn:uuid:file1"})).structured_content
    assert sc["pid"] == META and any("superseded" in n for n in sc["notes"])


async def test_compare_rejects_unknown_schema_ids_and_bad_counts(client, mock_api):
    mock_dataset(mock_api)
    with pytest.raises(ToolError, match="Unknown schema_id"):
        await client.call_tool("compare_eml_vs_data", {"pid": META, "schema_id": "S9"})
    with pytest.raises(ToolError, match="at least 1"):
        await client.call_tool("compare_eml_vs_data", {"pid": META, "files_per_schema": 0})


async def test_compare_rejects_documents_that_are_not_eml(client, mock_api):
    mock_dataset(mock_api)
    mock_api.get(f"{MN}/object/doi%3A10.18739%2FCMP").mock(return_value=httpx.Response(200, text="<gmd:MD_Metadata xmlns:gmd='x'/>"))
    with pytest.raises(ToolError, match="not EML"):
        await client.call_tool("compare_eml_vs_data", {"pid": META})


async def test_compare_skips_the_rest_of_a_group_after_a_timeout(client, mock_api, monkeypatch):
    from arcticdata_mcp.tools import compare as tool

    async def slow(client, limits, pid, scan="sample", header_rows="auto"):
        raise ToolError("Profiling took longer than 120 seconds; try scan='sample' or a smaller file.")

    monkeypatch.setattr(tool, "profile_object", slow)
    mock_dataset(mock_api)
    sc = (await client.call_tool("compare_eml_vs_data", {"pid": META})).structured_content
    assert [f["status"] for f in sc["groups"][0]["files"]] == ["error", "skipped: an earlier file in this group timed out", "skipped: an earlier file in this group timed out"]
    assert sc["summary"]["files_failed"] == 1 and sc["summary"]["files_skipped"] == 2
    assert any("timed out" in n for n in sc["notes"]) and any("No file could be profiled" in n for n in sc["notes"])


async def test_compare_says_so_when_the_eml_has_no_entities(client, mock_api):
    mock_dataset(mock_api, n_files=0)
    sc = (await client.call_tool("compare_eml_vs_data", {"pid": META})).structured_content
    assert [f["kind"] for f in sc["findings"]] == ["no_entities"] and sc["summary"]["entities"] == 0
