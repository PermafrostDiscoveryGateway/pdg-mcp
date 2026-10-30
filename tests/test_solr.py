import pytest
from fastmcp.exceptions import ToolError

from arcticdata_mcp import solr


def test_phrase_escapes_quotes():
    assert solr.phrase('a "b"') == '"a \\"b\\""'


def test_term_wildcard_escapes_specials_and_spaces():
    assert solr.term("*snow depth*") == "*snow\\ depth*"
    assert solr.term("http://x/y*") == "http\\:\\/\\/x\\/y*"


def test_term_rejects_empty():
    with pytest.raises(ToolError):
        solr.term("  ")


@pytest.mark.parametrize(
    ("value", "end", "expected"),
    [
        ("2010", False, "2010-01-01T00:00:00Z"),
        ("2010", True, "2010-12-31T23:59:59Z"),
        ("2012-02", True, "2012-02-29T23:59:59Z"),
        ("2012-02-03", False, "2012-02-03T00:00:00Z"),
        ("now-1year", False, "NOW-1YEAR"),
        (None, False, "*"),
        ("2020-01-01T12:00:00", False, "2020-01-01T12:00:00Z"),
    ],
)
def test_solr_date(value, end, expected):
    assert solr.solr_date(value, end_of_period=end) == expected


def test_solr_date_rejects_garbage():
    with pytest.raises(ToolError):
        solr.solr_date("last tuesday")


def test_bbox_intersects_and_within():
    inter = solr.bbox_clause(71, 68, -141, -166)
    assert "southBoundCoord:[* TO 71]" in inter and "eastBoundCoord:[-166 TO *]" in inter
    within = solr.bbox_clause(71, 68, -141, -166, mode="within")
    assert "southBoundCoord:[68 TO *]" in within and "eastBoundCoord:[* TO -141]" in within


def test_bbox_validates():
    with pytest.raises(ToolError):
        solr.bbox_clause(60, 70, 0, 0)


def test_missing_clause_negates_the_present_clause():
    assert solr.missing_clause("keywords") == "-keywords:*"
    assert solr.missing_clause("funding") == "-(awardNumber:* OR funderName:* OR fundingText:*)"


def test_missing_clause_rejects_unknown_field():
    with pytest.raises(ToolError, match="Unknown metadata field 'license'"):
        solr.missing_clause("license")


def test_missing_filters_all_is_one_filter_per_field():
    assert solr.missing_filters(["keywords", "spatial_coverage"], "all") == ["-keywords:*", "-northBoundCoord:*"]


def test_missing_filters_any_anchors_each_negative_clause_on_match_all():
    # A bare "-a OR -b" matches nothing useful in Solr (88 records instead of 605 on the live index).
    assert solr.missing_filters(["keywords", "spatial_coverage"], "any") == [
        "(*:* AND -keywords:*) OR (*:* AND -northBoundCoord:*)"
    ]
    assert solr.missing_filters(["keywords"], "any") == ["-keywords:*"]


def test_eml_format_filter_covers_both_eml_namespaces():
    assert solr.EML_FORMAT_FQ == r"formatId:(eml\:\/\/* OR https\:\/\/eml.ecoinformatics.org\/*)"
