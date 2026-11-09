import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "usage_report.py"
spec = importlib.util.spec_from_file_location("usage_report", SCRIPT)
usage_report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage_report)

PROM = "http://prom.test"
START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 11, 1, tzinfo=UTC)
WINDOW = "[2678400s]"  # 31 days


def vec(*items):
    """A Prometheus instant-vector response; items are (labels, value)."""
    return {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"metric": m, "value": [1, str(v)]} for m, v in items]},
    }


def fake_prometheus(request: httpx.Request) -> httpx.Response:
    q = request.url.params["query"]
    scalars = {  # substring -> value; checked most specific first
        'status="ok"}': 90,
        'status="upstream_busy"}': 2,
        "pdg_mcp_rate_limit_rejections_total": 4,
        'status=~"4.."': 7,
        'status=~"5.."': 3,
        'status="error"}': 1,
        "pdg_mcp_adc_retries_total": 5,
        "pdg_mcp_adc_requests_total": 120,
    }
    if q.startswith("sum(increase("):
        for needle, value in scalars.items():
            if needle in q:
                return httpx.Response(200, json=vec(({}, value)))
        return httpx.Response(200, json=vec(({}, 100)))  # total tool calls
    if q.startswith("histogram_quantile"):
        return httpx.Response(200, json=vec(({"tool": "search_datasets"}, 1.23456), ({"tool": "show_lakes"}, "NaN")))
    if "status!=" in q:
        return httpx.Response(200, json=vec(({"tool": "search_datasets"}, 6.2)))
    return httpx.Response(
        200,
        json=vec(({"tool": "show_lakes"}, 0), ({"tool": "search_datasets"}, 80.4), ({"tool": "get_portal"}, 19.6)),
    )


@pytest.fixture
def prom():
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{PROM}/api/v1/query").mock(side_effect=fake_prometheus)
        router.route_obj = route
        yield router


def report(prom):
    with httpx.Client() as c:
        return usage_report.fetch_report(c, PROM, START, END)


def test_summary_numbers(prom):
    s = report(prom)["summary"]
    assert s["tool_calls"] == 100
    assert s["tool_calls_ok"] == 90
    assert s["success_rate"] == 0.9
    assert s["rate_limit_rejections"] == 4
    assert s["upstream_busy_calls"] == 2
    assert (s["adc_4xx"], s["adc_5xx"], s["adc_no_response"], s["adc_retries"]) == (7, 3, 1, 5)
    assert s["adc_requests"] == 120


def test_per_tool_table_merges_and_sorts(prom):
    tools = report(prom)["tools"]
    assert [t["tool"] for t in tools] == ["search_datasets", "get_portal", "show_lakes"]  # unused tool last
    assert tools[0] == {"tool": "search_datasets", "calls": 80, "not_ok": 6, "p95_seconds": 1.235}
    assert tools[1] == {"tool": "get_portal", "calls": 20, "not_ok": 0, "p95_seconds": None}  # no failures, no p95
    assert tools[2]["calls"] == 0 and tools[2]["p95_seconds"] is None  # NaN p95 becomes None


def test_queries_use_the_period_as_range_evaluated_at_end(prom):
    report(prom)
    calls = prom.route_obj.calls
    assert all(WINDOW in c.request.url.params["query"] for c in calls)
    assert all(float(c.request.url.params["time"]) == END.timestamp() for c in calls)


def test_no_data_means_zero_not_an_error():
    empty = {"status": "success", "data": {"resultType": "vector", "result": []}}
    with respx.mock() as router:
        router.get(f"{PROM}/api/v1/query").mock(return_value=httpx.Response(200, json=empty))
        with httpx.Client() as c:
            r = usage_report.fetch_report(c, PROM, START, END)
    assert r["summary"]["tool_calls"] == 0
    assert r["summary"]["success_rate"] is None
    assert r["tools"] == []


def test_prometheus_errors_are_reported():
    with respx.mock() as router:
        router.get(f"{PROM}/api/v1/query").mock(return_value=httpx.Response(500, text="boom"))
        with httpx.Client() as c, pytest.raises(usage_report.PrometheusError, match="Prometheus query failed"):
            usage_report.fetch_report(c, PROM, START, END)
    with respx.mock() as router:
        router.get(f"{PROM}/api/v1/query").mock(
            return_value=httpx.Response(200, json={"status": "error", "error": "bad expr"})
        )
        with httpx.Client() as c, pytest.raises(usage_report.PrometheusError, match="bad expr"):
            usage_report.fetch_report(c, PROM, START, END)


def test_parse_when():
    assert usage_report.parse_when("2026-10-01") == START
    assert usage_report.parse_when("2026-10-01T12:30:00+00:00") == datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
    assert usage_report.parse_when("2026-10-01T12:30:00").tzinfo is UTC
    assert usage_report.parse_when("now").tzinfo is not None
    with pytest.raises(Exception, match="not a date"):
        usage_report.parse_when("last tuesday")


def test_outputs(prom):
    r = report(prom)
    table = usage_report.render(r, "table", "all")
    assert "Tool calls" in table and "90.0%" in table and "search_datasets" in table
    assert "Active users are not tracked yet" in table

    csv_tools = usage_report.render(r, "csv", "tools").splitlines()
    assert csv_tools[0] == "tool,calls,not_ok,p95_seconds"
    assert csv_tools[1] == "search_datasets,80,6,1.235"
    assert csv_tools[-1] == "show_lakes,0,0,"

    csv_all = usage_report.render(r, "csv", "all").splitlines()
    assert csv_all[0] == "section,name,value,not_ok,p95_seconds"
    assert "summary,tool_calls,100,," in csv_all

    data = json.loads(usage_report.render(r, "json", "summary"))
    assert set(data) == {"period", "summary"} and data["summary"]["tool_calls"] == 100


def test_report_contains_no_user_data(prom):
    text = json.dumps(report(prom)).lower()
    assert "user" not in text.replace("rate_limit", "")  # only the explanatory table footer mentions users


def test_cli(prom, capsys):
    rc = usage_report.main(["--prometheus-url", PROM, "--from", "2026-10-01", "--to", "2026-11-01", "--format", "csv", "--section", "summary"])
    assert rc == 0
    assert "tool_calls,100" in capsys.readouterr().out


def test_cli_rejects_backwards_period(capsys):
    with pytest.raises(SystemExit):
        usage_report.main(["--from", "2026-11-01", "--to", "2026-10-01"])


def test_cli_reports_prometheus_down(capsys):
    with respx.mock() as router:
        router.get(f"{PROM}/api/v1/query").mock(side_effect=httpx.ConnectError("refused"))
        rc = usage_report.main(["--prometheus-url", PROM, "--from", "2026-10-01", "--to", "2026-11-01"])
    assert rc == 1
    assert "error:" in capsys.readouterr().err
