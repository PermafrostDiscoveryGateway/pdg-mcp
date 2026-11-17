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


# --- archive mode -------------------------------------------------------------------------------------------
NOW = datetime(2026, 11, 10, 1, 15, tzinfo=UTC)  # a day's job runs shortly after midnight UTC
EMPTY = {"status": "success", "data": {"resultType": "vector", "result": []}}


def day_end(day: int) -> float:
    return datetime(2026, 11, day, tzinfo=UTC).timestamp()


def archive_router(router, nodata_days=()):
    """Mock Prometheus; the days in ``nodata_days`` (their end = midnight starting that day + 1) have no samples."""
    nodata_ends = {day_end(d + 1) for d in nodata_days}

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params["query"]
        if q.startswith("count(count_over_time(") and float(request.url.params["time"]) in nodata_ends:
            return httpx.Response(200, json=EMPTY)
        if q.startswith("count(count_over_time("):
            return httpx.Response(200, json=vec(({}, 1)))
        return fake_prometheus(request)

    return router.get(f"{PROM}/api/v1/query").mock(side_effect=handler)


def run_archive(tmp_path, days=3, force=False):
    with httpx.Client() as c:
        return usage_report.archive(c, PROM, tmp_path, days, NOW, force=force)


def test_archive_writes_one_file_per_finished_day(tmp_path):
    with respx.mock() as router:
        archive_router(router)
        lines, ok = run_archive(tmp_path)
    assert ok is True
    assert lines == ["2026-11-07: archived", "2026-11-08: archived", "2026-11-09: archived"]
    assert sorted(p.name for p in tmp_path.glob("usage-*.json")) == [
        "usage-2026-11-07.json", "usage-2026-11-08.json", "usage-2026-11-09.json"]
    day = json.loads((tmp_path / "usage-2026-11-09.json").read_text())
    assert day["period"] == {"from": "2026-11-09T00:00:00+00:00", "to": "2026-11-10T00:00:00+00:00"}
    assert day["summary"]["tool_calls"] == 100 and day["tools"][0]["tool"] == "search_datasets"
    assert not list(tmp_path.glob("*.tmp"))  # atomic writes leave nothing behind


def test_archive_queries_a_full_day_ending_at_midnight(tmp_path):
    with respx.mock() as router:
        route = archive_router(router)
        run_archive(tmp_path, days=1)
    queries = [(c.request.url.params["query"], float(c.request.url.params["time"])) for c in route.calls]
    assert queries and all("[86400s]" in q for q, _ in queries)
    assert all(t == day_end(10) for _, t in queries)  # evaluated at the end of 2026-11-09


def test_archive_rerun_skips_finished_days_and_force_rewrites(tmp_path):
    with respx.mock() as router:
        route = archive_router(router)
        run_archive(tmp_path)
        calls_after_first = route.call_count
        lines, ok = run_archive(tmp_path)
        assert ok and set(lines) == {f"2026-11-0{d}: already archived" for d in (7, 8, 9)}
        assert route.call_count == calls_after_first  # nothing was queried again
        lines, _ = run_archive(tmp_path, force=True)
        assert all(line.endswith("archived") and "already" not in line for line in lines)
        assert route.call_count > calls_after_first


def test_a_day_without_data_is_not_archived_as_zeros(tmp_path):
    with respx.mock() as router:
        archive_router(router, nodata_days=[7])  # e.g. aged out of Prometheus's retention
        lines, ok = run_archive(tmp_path)
    assert "no data" in lines[0] and lines[0].startswith("2026-11-07")
    assert not (tmp_path / "usage-2026-11-07.json").exists()
    assert ok is True  # only a missing *yesterday* is a failure
    assert (tmp_path / "usage-2026-11-08.json").exists()


def test_nothing_at_all_is_created_when_no_day_has_data(tmp_path):
    target = tmp_path / "fresh"
    with respx.mock() as router:
        router.get(f"{PROM}/api/v1/query").mock(return_value=httpx.Response(200, json=EMPTY))
        with httpx.Client() as c:
            lines, ok = usage_report.archive(c, PROM, target, 3, NOW)
    assert ok is False and all("no data" in line for line in lines)
    assert not target.exists()  # no directory, no day files, no empty CSVs


def test_missing_yesterday_is_reported_as_failure(tmp_path):
    with respx.mock() as router:
        archive_router(router, nodata_days=[9])
        lines, ok = run_archive(tmp_path)
    assert ok is False and "no data" in lines[-1]
    assert not (tmp_path / "usage-2026-11-09.json").exists()


def test_archive_catches_up_a_missed_run(tmp_path):
    """Only the 9th is archived (the earlier runs were missed); a later run with --days 3 fills the gap."""
    with respx.mock() as router:
        archive_router(router)
        run_archive(tmp_path, days=1)
        lines, ok = run_archive(tmp_path, days=3)
    assert ok and lines == ["2026-11-07: archived", "2026-11-08: archived", "2026-11-09: already archived"]


def test_csvs_are_rebuilt_from_the_daily_files_without_duplicates(tmp_path):
    with respx.mock() as router:
        archive_router(router)
        run_archive(tmp_path)
        first = (tmp_path / "daily-summary.csv").read_text()
        run_archive(tmp_path)  # a re-run must not add rows
        run_archive(tmp_path, force=True)
    summary = (tmp_path / "daily-summary.csv").read_text().splitlines()
    assert summary[0].split(",")[:3] == ["date", "tool_calls", "tool_calls_ok"]
    assert [row.split(",")[0] for row in summary[1:]] == ["2026-11-07", "2026-11-08", "2026-11-09"]
    assert (tmp_path / "daily-summary.csv").read_text() == first
    tools = (tmp_path / "daily-tools.csv").read_text().splitlines()
    assert tools[0] == "date,tool,calls,not_ok,p95_seconds"
    assert "2026-11-08,search_datasets,80,6,1.235" in tools
    assert len(tools) == 1 + 3 * 3  # header + 3 tools per day


def test_cli_archive_mode(tmp_path, capsys):
    with respx.mock() as router:
        archive_router(router)
        rc = usage_report.main(["--prometheus-url", PROM, "--archive-dir", str(tmp_path / "out"), "--days", "2"])
    assert rc == 0
    assert len(list((tmp_path / "out").glob("usage-*.json"))) == 2
    assert capsys.readouterr().out.count("archived") == 2


def test_cli_archive_exits_nonzero_when_yesterday_has_no_data(tmp_path, capsys):
    with respx.mock() as router:
        router.get(f"{PROM}/api/v1/query").mock(return_value=httpx.Response(200, json=EMPTY))
        rc = usage_report.main(["--prometheus-url", PROM, "--archive-dir", str(tmp_path), "--days", "2"])
    assert rc == 1
    assert "yesterday is not archived" in capsys.readouterr().err
    assert not list(tmp_path.glob("usage-*.json"))


def test_cli_archive_reports_prometheus_down(tmp_path, capsys):
    with respx.mock() as router:
        router.get(f"{PROM}/api/v1/query").mock(side_effect=httpx.ConnectError("refused"))
        rc = usage_report.main(["--prometheus-url", PROM, "--archive-dir", str(tmp_path)])
    assert rc == 1 and "error:" in capsys.readouterr().err


def test_cli_archive_rejects_from_and_to_and_bad_days(tmp_path):
    with pytest.raises(SystemExit):
        usage_report.main(["--archive-dir", str(tmp_path), "--from", "2026-10-01"])
    with pytest.raises(SystemExit):
        usage_report.main(["--archive-dir", str(tmp_path), "--days", "0"])
