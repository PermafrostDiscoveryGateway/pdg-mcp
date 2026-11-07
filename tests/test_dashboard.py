import json
import re
import sys
from pathlib import Path

import pytest

from arcticdata_mcp.metrics import Metrics

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboards" / "pdg-mcp.json"


@pytest.fixture(scope="module")
def dashboard():
    return json.loads(DASHBOARD.read_text())


def all_exprs(dashboard):
    return [t["expr"] for p in dashboard["panels"] for t in p.get("targets", [])]


def test_every_metric_in_the_dashboard_exists(dashboard):
    """Guards against a metric being renamed in metrics.py without updating the dashboard."""
    exposed = set(re.findall(r"^# HELP (\S+)", Metrics().render().decode(), re.M))
    used = {
        re.sub(r"_(bucket|sum|count)$", "", name)
        for expr in all_exprs(dashboard)
        for name in re.findall(r"\bpdg_mcp_[a-z0-9_]+", expr)
    }
    if not sys.platform.startswith("linux"):  # process metrics are only exported where /proc exists
        used = {n for n in used if not n.startswith("pdg_mcp_process_")}
    assert used, "dashboard uses no pdg_mcp_ metrics?"
    assert used <= exposed, f"not exported by the server: {sorted(used - exposed)}"


def test_dashboard_structure(dashboard):
    assert dashboard["uid"] == "pdg-mcp-usage"
    assert dashboard["schemaVersion"] >= 41  # Grafana 12
    ids = [p["id"] for p in dashboard["panels"]]
    assert len(ids) == len(set(ids))
    for p in dashboard["panels"]:
        if p["type"] not in ("row", "text"):
            assert p["targets"], p["title"]
            assert p["datasource"]["uid"] == "${datasource}", p["title"]  # no hard-coded datasource
    assert {v["name"] for v in dashboard["templating"]["list"]} == {"datasource", "tool"}


def test_dashboard_has_no_per_user_panels(dashboard):
    """Policy: dashboards show aggregates only."""
    text = json.dumps(all_exprs(dashboard)).lower()
    assert "user_hash" not in text and "by (user" not in text


def test_summed_legends_use_non_overlapping_windows(dashboard):
    """A legend 'sum' over rate_interval windows (which overlap) would count the same calls repeatedly."""
    for p in dashboard["panels"]:
        if "sum" in p.get("options", {}).get("legend", {}).get("calcs", []):
            for t in p["targets"]:
                assert "$__rate_interval" not in t["expr"], p["title"]
                assert "$__interval" in t["expr"], p["title"]
