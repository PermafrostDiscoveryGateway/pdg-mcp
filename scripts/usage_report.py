"""Print usage numbers for a time period from Prometheus, for reports.

Reads the metrics the server exports (see "Metrics" in the README) through Prometheus's HTTP API and prints
totals and a per-tool table. Output is aggregate counts only, never per-user data.

    uv run python scripts/usage_report.py --prometheus-url http://localhost:9090 \
        --from 2026-10-01 --to 2026-11-01

    uv run python scripts/usage_report.py --from 2026-10-01 --format csv --section tools > tools.csv

``--from`` / ``--to`` take ``YYYY-MM-DD`` (midnight UTC), an ISO-8601 timestamp, or ``now``. The default period is
the last 30 days. Set ``PROMETHEUS_TOKEN`` to send ``Authorization: Bearer <token>``.

Caveats: numbers come from Prometheus, so they only reach back as far as its retention. ``increase()``
extrapolates slightly at the edges of the period, so counts can be off by a small fraction of a call.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

DEFAULT_URL = "http://localhost:9090"
CALLS = "pdg_mcp_tool_calls_total"


# --- time handling ------------------------------------------------------------------------------------------
def parse_when(value: str) -> datetime:
    """``now``, ``YYYY-MM-DD`` or an ISO-8601 timestamp; naive values are taken as UTC."""
    if value == "now":
        return datetime.now(UTC)
    try:
        when = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a date or ISO-8601 timestamp: {value!r}") from exc
    return when if when.tzinfo else when.replace(tzinfo=UTC)


# --- querying -----------------------------------------------------------------------------------------------
class PrometheusError(RuntimeError):
    pass


def _number(raw: str) -> float | None:
    value = float(raw)
    return None if math.isnan(value) else value


def query(client: httpx.Client, base_url: str, expr: str, at: datetime) -> list[dict[str, Any]]:
    """Run an instant query; returns the result vector (``[]`` when no series match)."""
    try:
        resp = client.get(f"{base_url.rstrip('/')}/api/v1/query", params={"query": expr, "time": at.timestamp()})
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise PrometheusError(f"Prometheus query failed ({expr}): {exc}") from exc
    if body.get("status") != "success":
        raise PrometheusError(f"Prometheus query failed ({expr}): {body.get('error', body)}")
    return body["data"]["result"]


def scalar(client: httpx.Client, base_url: str, expr: str, at: datetime) -> float:
    """First value of a query, 0 when there is no data (no calls in the period means no series)."""
    result = query(client, base_url, expr, at)
    value = _number(result[0]["value"][1]) if result else None
    return 0.0 if value is None else value


def by_tool(client: httpx.Client, base_url: str, expr: str, at: datetime) -> dict[str, float | None]:
    return {r["metric"].get("tool", ""): _number(r["value"][1]) for r in query(client, base_url, expr, at)}


def fetch_report(client: httpx.Client, base_url: str, start: datetime, end: datetime) -> dict[str, Any]:
    """Collect the report for ``start`` .. ``end`` by evaluating ``increase()`` over the period at ``end``."""
    window = f"[{int((end - start).total_seconds())}s]"

    def total(expr: str) -> int:
        return round(scalar(client, base_url, f"sum(increase({expr}{window}))", end))

    calls = total(CALLS)
    ok = total(f'{CALLS}{{status="ok"}}')
    summary: dict[str, float | int | None] = {
        "tool_calls": calls,
        "tool_calls_ok": ok,
        "success_rate": round(ok / calls, 4) if calls else None,
        "rate_limit_rejections": total("pdg_mcp_rate_limit_rejections_total"),
        "upstream_busy_calls": total(f'{CALLS}{{status="upstream_busy"}}'),
        "adc_requests": total("pdg_mcp_adc_requests_total"),
        "adc_4xx": total('pdg_mcp_adc_requests_total{status=~"4.."}'),
        "adc_5xx": total('pdg_mcp_adc_requests_total{status=~"5.."}'),
        "adc_no_response": total('pdg_mcp_adc_requests_total{status="error"}'),
        "adc_retries": total("pdg_mcp_adc_retries_total"),
    }

    per_calls = by_tool(client, base_url, f"sum by (tool) (increase({CALLS}{window}))", end)
    per_not_ok = by_tool(client, base_url, f'sum by (tool) (increase({CALLS}{{status!="ok"}}{window}))', end)
    per_p95 = by_tool(
        client,
        base_url,
        f"histogram_quantile(0.95, sum by (le, tool) (increase(pdg_mcp_tool_call_duration_seconds_bucket{window})))",
        end,
    )
    tools = [
        {
            "tool": tool,
            "calls": round(per_calls[tool] or 0),
            "not_ok": round(per_not_ok.get(tool) or 0),
            "p95_seconds": None if per_p95.get(tool) is None else round(per_p95[tool], 3),
        }
        for tool in per_calls
    ]
    tools.sort(key=lambda t: (-t["calls"], t["tool"]))  # unused tools end up at the bottom
    return {
        "period": {"from": start.isoformat(), "to": end.isoformat()},
        "summary": summary,
        "tools": tools,
    }


# --- output -------------------------------------------------------------------------------------------------
SUMMARY_LABELS = {
    "tool_calls": "Tool calls",
    "tool_calls_ok": "Tool calls that succeeded",
    "success_rate": "Success rate",
    "rate_limit_rejections": "Calls rejected by the rate limit",
    "upstream_busy_calls": "Calls that failed because ADC was busy",
    "adc_requests": "Requests to ADC (every attempt)",
    "adc_4xx": "ADC 4xx responses",
    "adc_5xx": "ADC 5xx responses",
    "adc_no_response": "ADC requests with no response",
    "adc_retries": "ADC requests retried",
}


def _fmt(value: float | int | None, *, pct: bool = False) -> str:
    if value is None:
        return "-"
    return f"{value:.1%}" if pct else f"{value:,}"


def render_table(report: dict[str, Any], section: str) -> str:
    out = [f"Usage report: {report['period']['from']} to {report['period']['to']}", ""]
    if section in ("summary", "all"):
        width = max(len(v) for v in SUMMARY_LABELS.values())
        for key, label in SUMMARY_LABELS.items():
            value = report["summary"][key]
            out.append(f"  {label:<{width}}  {_fmt(value, pct=key == 'success_rate'):>10}")
        out.append("")
    if section in ("tools", "all"):
        name_w = max([len("Tool")] + [len(t["tool"]) for t in report["tools"]])
        out.append(f"  {'Tool':<{name_w}}  {'Calls':>9}  {'Not ok':>7}  {'p95 (s)':>8}")
        for t in report["tools"]:
            p95 = "-" if t["p95_seconds"] is None else f"{t['p95_seconds']:.2f}"
            out.append(f"  {t['tool']:<{name_w}}  {t['calls']:>9,}  {t['not_ok']:>7,}  {p95:>8}")
        out.append("")
    out.append("Active users are not tracked yet. Counts come from Prometheus and are limited to its retention.")
    out.append("ADC request counts can run slightly low after a server restart; tool-call counts are not affected.")
    return "\n".join(out)


def render_csv(report: dict[str, Any], section: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    if section == "tools":
        w.writerow(["tool", "calls", "not_ok", "p95_seconds"])
        for t in report["tools"]:
            w.writerow([t["tool"], t["calls"], t["not_ok"], "" if t["p95_seconds"] is None else t["p95_seconds"]])
    elif section == "summary":
        w.writerow(["metric", "value"])
        for key in SUMMARY_LABELS:
            value = report["summary"][key]
            w.writerow([key, "" if value is None else value])
    else:  # all: one long table so the file stays a single CSV
        w.writerow(["section", "name", "value", "not_ok", "p95_seconds"])
        for key in SUMMARY_LABELS:
            value = report["summary"][key]
            w.writerow(["summary", key, "" if value is None else value, "", ""])
        for t in report["tools"]:
            w.writerow(["tool", t["tool"], t["calls"], t["not_ok"], "" if t["p95_seconds"] is None else t["p95_seconds"]])
    return buf.getvalue()


def render(report: dict[str, Any], fmt: str, section: str) -> str:
    if fmt == "json":
        data = {"period": report["period"]}
        if section in ("summary", "all"):
            data["summary"] = report["summary"]
        if section in ("tools", "all"):
            data["tools"] = report["tools"]
        return json.dumps(data, indent=2)
    if fmt == "csv":
        return render_csv(report, section)
    return render_table(report, section)


# --- CLI ----------------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Usage numbers for a time period, from Prometheus.")
    p.add_argument("--prometheus-url", default=os.environ.get("PROMETHEUS_URL", DEFAULT_URL),
                   help=f"Prometheus base URL (default: $PROMETHEUS_URL or {DEFAULT_URL})")
    p.add_argument("--from", dest="start", type=parse_when, default=None, help="start (default: 30 days before --to)")
    p.add_argument("--to", dest="end", type=parse_when, default=None, help="end (default: now)")
    p.add_argument("--format", choices=["table", "csv", "json"], default="table")
    p.add_argument("--section", choices=["summary", "tools", "all"], default="all")
    args = p.parse_args(argv)

    end = args.end or datetime.now(UTC)
    start = args.start or end - timedelta(days=30)
    if start >= end:
        p.error("--from must be earlier than --to")

    headers = {"Authorization": f"Bearer {os.environ['PROMETHEUS_TOKEN']}"} if os.environ.get("PROMETHEUS_TOKEN") else {}
    try:
        with httpx.Client(timeout=30, headers=headers) as client:
            report = fetch_report(client, args.prometheus_url, start, end)
    except PrometheusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(render(report, args.format, args.section))
    return 0


if __name__ == "__main__":
    sys.exit(main())
