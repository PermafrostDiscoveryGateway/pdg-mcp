"""Active users: the per-day store, the pdg_mcp_active_users gauge and how the server wires them up."""

import logging
import sqlite3
from datetime import date, timedelta

import httpx
import pytest
from conftest import MN
from fastmcp import Client
from fastmcp.exceptions import ToolError

from arcticdata_mcp import activeusers
from arcticdata_mcp.activeusers import MIN_RETENTION_DAYS, ActiveUsers
from arcticdata_mcp.config import ObservabilitySettings
from arcticdata_mcp.metrics import Metrics
from arcticdata_mcp.observability import hash_user
from arcticdata_mcp.ratelimit import ANONYMOUS, UserRateLimitMiddleware
from arcticdata_mcp.server import build_active_users, create_server

SOLR_OK = {"response": {"numFound": 0, "start": 0, "docs": []}}
DAY = date(2026, 11, 20)


def gauge(metrics: Metrics) -> dict[str, float]:
    """The pdg_mcp_active_users samples, keyed by period."""
    out = {}
    for line in metrics.render().decode().splitlines():
        if line.startswith("pdg_mcp_active_users{"):
            labels, value = line.rsplit(" ", 1)
            out[labels.split('period="')[1].split('"')[0]] = float(value)
    return out


# --- the store -----------------------------------------------------------------------------------------------
def test_a_user_counts_once_per_day():
    s = ActiveUsers()
    for _ in range(3):
        s.record("aaaa", DAY)
    s.record("bbbb", DAY)
    assert s.count(DAY, DAY) == 2


def test_anonymous_callers_are_not_users():
    s = ActiveUsers()
    s.record(ANONYMOUS, DAY)
    s.record("", DAY)
    assert s.count(DAY, DAY) == 0


def test_a_user_on_several_days_counts_once_over_the_range():
    s = ActiveUsers()
    for back in range(5):
        s.record("aaaa", DAY - timedelta(days=back))
    s.record("bbbb", DAY - timedelta(days=2))
    assert s.count(DAY - timedelta(days=4), DAY) == 2
    assert s.count(DAY, DAY) == 1


def test_counts_by_period():
    s = ActiveUsers()
    s.record("today", DAY)
    s.record("five-days-ago", DAY - timedelta(days=5))
    s.record("ten-days-ago", DAY - timedelta(days=10))  # Nov 10: same month, outside 7d
    s.record("last-month", date(2026, 10, 25))  # inside 30d, not this month
    s.record("too-old", DAY - timedelta(days=30))  # just outside 30d (today counts as day 1)
    assert s.counts(DAY) == {"day": 1, "month": 3, "7d": 2, "30d": 4}


def test_old_rows_are_deleted_on_a_new_day():
    s = ActiveUsers(retention_days=35)
    s.record("old", DAY - timedelta(days=36))
    s.record("kept", DAY - timedelta(days=35))
    s.record("new", DAY)  # first record of a new day prunes
    with s._lock:
        days = [r[0] for r in s._conn.execute("SELECT day FROM user_days ORDER BY day")]
    assert days == [(DAY - timedelta(days=35)).isoformat(), DAY.isoformat()]


def test_retention_too_short_for_monthly_counts_is_refused():
    with pytest.raises(ValueError, match="at least 31"):
        ActiveUsers(retention_days=MIN_RETENTION_DAYS - 1)


def test_counts_survive_a_restart(tmp_path):
    db = tmp_path / "sub" / "au.sqlite"  # the directory is created
    first = ActiveUsers(str(db))
    first.record("aaaa", DAY)
    first.close()
    second = ActiveUsers(str(db))
    second.record("aaaa", DAY)  # same user after the restart: still one
    second.record("bbbb", DAY)
    assert second.count(DAY, DAY) == 2


def test_only_hashes_and_days_are_stored(tmp_path):
    db = tmp_path / "au.sqlite"
    s = ActiveUsers(str(db))
    s.record("aaaa", DAY)
    s.close()
    conn = sqlite3.connect(db)
    assert [c[1] for c in conn.execute("PRAGMA table_info(user_days)")] == ["day", "user_hash"]
    assert conn.execute("SELECT * FROM user_days").fetchall() == [(DAY.isoformat(), "aaaa")]


def test_a_failing_database_does_not_break_the_call(caplog):
    s = ActiveUsers()
    s._conn.close()  # every write now raises
    with caplog.at_level(logging.WARNING, logger=activeusers.log.name):
        s.record("aaaa", DAY)
        s.record("bbbb", DAY)
    assert sum("Could not record" in r.message for r in caplog.records) == 1  # warned once, no exception


# --- the gauge ------------------------------------------------------------------------------------------------
def test_gauge_is_described_without_a_store():
    text = Metrics().render().decode()
    assert "# HELP pdg_mcp_active_users" in text
    assert "pdg_mcp_active_users{" not in text  # no samples


def test_gauge_reports_counts_only(monkeypatch):
    m = Metrics()
    s = ActiveUsers()
    monkeypatch.setattr(activeusers, "_today", lambda: DAY)
    s.record("secret-hash-1", DAY)
    s.record("secret-hash-2", DAY - timedelta(days=3))
    m.attach_active_users(s)
    assert gauge(m) == {"day": 1, "month": 2, "7d": 2, "30d": 2}
    assert "secret-hash" not in m.render().decode()


def test_a_broken_store_does_not_break_the_scrape():
    m = Metrics()
    s = ActiveUsers()
    s._conn.close()
    m.attach_active_users(s)
    text = m.render().decode()
    assert "pdg_mcp_tool_calls_total" in text and "pdg_mcp_active_users{" not in text


# --- wiring in the server ----------------------------------------------------------------------------------------
async def test_tool_calls_record_the_hashed_user(settings, mock_api, monkeypatch):
    settings.observability.user_hash_key = "k1"
    monkeypatch.setattr(UserRateLimitMiddleware, "user_key", staticmethod(lambda: "sub-1234"))
    mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(200, json=SOLR_OK))
    store, metrics = ActiveUsers(), Metrics()
    async with Client(create_server(settings, require_auth=False, metrics=metrics, active_users=store)) as c:
        await c.call_tool("count_datasets", {"text": "x"})
        await c.call_tool("count_datasets", {"text": "y"})
        with pytest.raises(ToolError):
            await c.call_tool("made_up_tool", {})  # a failed call still means the user was active
    with store._lock:
        rows = store._conn.execute("SELECT user_hash FROM user_days").fetchall()
    assert rows == [(hash_user("sub-1234", "k1"),)]
    assert gauge(metrics)["day"] == 1


async def test_anonymous_server_counts_no_users(settings, mock_api):
    mock_api.get(f"{MN}/query/solr/").mock(return_value=httpx.Response(200, json=SOLR_OK))
    store, metrics = ActiveUsers(), Metrics()
    async with Client(create_server(settings, require_auth=False, metrics=metrics, active_users=store)) as c:
        await c.call_tool("count_datasets", {"text": "x"})
    assert gauge(metrics)["day"] == 0


def test_no_store_without_authentication():
    assert build_active_users(ObservabilitySettings(), require_auth=False) is None


def test_no_store_when_disabled():
    assert build_active_users(ObservabilitySettings(active_users_enabled=False), require_auth=True) is None


def test_database_file_needs_a_stable_hash_key(tmp_path, caplog):
    obs = ObservabilitySettings(active_users_db=str(tmp_path / "au.sqlite"))
    with caplog.at_level(logging.WARNING):
        store = build_active_users(obs, require_auth=True)
    assert store is not None and not store.persistent
    assert not (tmp_path / "au.sqlite").exists()
    assert "user_hash_key is not" in caplog.text


def test_database_file_is_used_with_a_hash_key(tmp_path):
    obs = ObservabilitySettings(active_users_db=str(tmp_path / "au.sqlite"), user_hash_key="k")
    store = build_active_users(obs, require_auth=True)
    assert store is not None and store.persistent and (tmp_path / "au.sqlite").exists()


def test_retention_setting_is_passed_through():
    store = build_active_users(ObservabilitySettings(active_users_retention_days=60), require_auth=True)
    assert store is not None and store.retention_days == 60


# --- command line -----------------------------------------------------------------------------------------------
def test_cli_prints_a_count_and_never_hashes(tmp_path, capsys, monkeypatch):
    db = tmp_path / "au.sqlite"
    s = ActiveUsers(str(db))
    for back, user in [(0, "hash-a"), (1, "hash-a"), (2, "hash-b"), (20, "hash-c")]:
        s.record(user, DAY - timedelta(days=back))
    s.close()
    monkeypatch.setattr(activeusers, "_today", lambda: DAY)
    assert activeusers.main(["--db", str(db), "--from", "2026-11-18", "--to", "2026-11-20"]) == 0
    out = capsys.readouterr()
    assert out.out.strip().endswith(": 2") and "hash-" not in out.out
    assert out.err == ""


def test_cli_warns_when_the_range_starts_before_the_retention(tmp_path, capsys, monkeypatch):
    db = tmp_path / "au.sqlite"
    ActiveUsers(str(db)).close()
    monkeypatch.setattr(activeusers, "_today", lambda: DAY)
    assert activeusers.main(["--db", str(db), "--from", "2026-01-01"]) == 0
    assert "retention" in capsys.readouterr().err


def test_cli_never_creates_a_database(tmp_path):
    with pytest.raises(SystemExit):
        activeusers.main(["--db", str(tmp_path / "missing.sqlite"), "--from", "2026-11-01"])
    assert not (tmp_path / "missing.sqlite").exists()
