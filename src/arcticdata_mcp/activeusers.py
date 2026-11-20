"""Active users: which hashed users made a tool call on each UTC day.

Prometheus cannot count distinct users (a user label is ruled out by the privacy policy), so the server keeps a
small SQLite table of ``(day, user_hash)`` rows, one per user per UTC day, and exports only the counts as the
``pdg_mcp_active_users{period=...}`` gauge (see :mod:`arcticdata_mcp.metrics`). Rows older than the retention
period are deleted, so the hashes themselves are kept only briefly; long-term numbers live in the daily archive as
counts.

With no database path the table lives in memory and the counts restart from zero when the process does.

For an exact count over any range still inside the retention period, run this module in the pod::

    python -m arcticdata_mcp.activeusers --from 2026-11-01 --to 2026-11-30

It prints counts only, never hashes.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from arcticdata_mcp.ratelimit import ANONYMOUS

log = logging.getLogger(__name__)

# Month-to-date reaches back up to 30 days (to the 1st of a 31-day month) and the rolling window is 30 days, so
# keeping fewer days would make those counts silently low.
MIN_RETENTION_DAYS = 31
# Rolling windows in whole UTC days, today included: "7d" is today and the 6 days before it.
ROLLING_WINDOWS = {"7d": 7, "30d": 30}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_days (
    day TEXT NOT NULL,        -- UTC date, YYYY-MM-DD
    user_hash TEXT NOT NULL,  -- keyed hash of the Keycloak sub, never the sub itself
    PRIMARY KEY (day, user_hash)
) WITHOUT ROWID
"""


def _today() -> date:
    return datetime.now(UTC).date()


class ActiveUsers:
    """Record users per UTC day and count distinct users over day ranges.

    Safe to use from the event loop and the metrics thread at once. A failing database never breaks a tool call:
    :meth:`record` logs the problem and carries on.
    """

    def __init__(self, path: str = "", retention_days: int = 35) -> None:
        if retention_days < MIN_RETENTION_DAYS:
            raise ValueError(
                f"observability.active_users_retention_days must be at least {MIN_RETENTION_DAYS}, got {retention_days}"
            )
        self.path = path
        self.retention_days = retention_days
        if path:
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(Path(path).expanduser() if path else ":memory:", check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()
        # Users already written today, so a busy user costs one database write per day, not one per call.
        self._seen_day: date | None = None
        self._seen: set[str] = set()
        self._failed = False
        self.prune()

    @property
    def persistent(self) -> bool:
        return bool(self.path)

    def record(self, user_hash: str, today: date | None = None) -> None:
        """Note that ``user_hash`` was active today. Anonymous callers are not users and are skipped."""
        if not user_hash or user_hash == ANONYMOUS:
            return
        today = today or _today()
        if today != self._seen_day:
            self._seen_day, self._seen = today, set()
            self.prune(today)
        if user_hash in self._seen:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR IGNORE INTO user_days (day, user_hash) VALUES (?, ?)", (today.isoformat(), user_hash)
                )
                self._conn.commit()
        except sqlite3.Error:
            if not self._failed:  # say it once, not on every call
                log.warning("Could not record an active user; active-user counts will be low", exc_info=True)
                self._failed = True
            return
        self._failed = False
        self._seen.add(user_hash)

    def prune(self, today: date | None = None) -> None:
        """Delete rows older than the retention period."""
        cutoff = (today or _today()) - timedelta(days=self.retention_days)
        try:
            with self._lock:
                self._conn.execute("DELETE FROM user_days WHERE day < ?", (cutoff.isoformat(),))
                self._conn.commit()
        except sqlite3.Error:
            log.warning("Could not delete old active-user rows", exc_info=True)

    def count(self, first: date, last: date) -> int:
        """Distinct users active on any UTC day from ``first`` to ``last``, both included."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(DISTINCT user_hash) FROM user_days WHERE day BETWEEN ? AND ?",
                (first.isoformat(), last.isoformat()),
            ).fetchone()
        return int(row[0])

    def counts(self, today: date | None = None) -> dict[str, int]:
        """The values of the ``pdg_mcp_active_users`` gauge, keyed by its ``period`` label."""
        today = today or _today()
        out = {"day": self.count(today, today), "month": self.count(today.replace(day=1), today)}
        for period, days in ROLLING_WINDOWS.items():
            out[period] = self.count(today - timedelta(days=days - 1), today)
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --- command line: exact counts for a range, read straight from the database ---------------------------------
def main(argv: list[str] | None = None) -> int:
    from arcticdata_mcp.config import load_settings

    p = argparse.ArgumentParser(
        description="Count distinct active users per period from the server's active-user database (counts only)."
    )
    p.add_argument("--config", "-c", default=None, help="config.toml to read observability.active_users_db from")
    p.add_argument("--db", default=None, help="database path (default: observability.active_users_db)")
    p.add_argument("--from", dest="first", type=date.fromisoformat, required=True, help="first UTC day, YYYY-MM-DD")
    p.add_argument("--to", dest="last", type=date.fromisoformat, default=None, help="last UTC day (default: today)")
    args = p.parse_args(argv)

    settings = load_settings(args.config).observability
    db = args.db or settings.active_users_db
    if not db:
        p.error("no database: pass --db or set observability.active_users_db")
    if not Path(db).expanduser().is_file():
        p.error(f"database not found: {db}")
    today = _today()
    last = args.last or today
    if args.first > last:
        p.error("--from must not be after --to")

    conn = sqlite3.connect(f"file:{Path(db).expanduser()}?mode=ro", uri=True)  # never write from here
    try:
        (n,) = conn.execute(
            "SELECT COUNT(DISTINCT user_hash) FROM user_days WHERE day BETWEEN ? AND ?",
            (args.first.isoformat(), last.isoformat()),
        ).fetchone()
    finally:
        conn.close()
    print(f"Distinct active users {args.first} to {last} (UTC days, inclusive): {n}")
    kept_from = today - timedelta(days=settings.active_users_retention_days)
    if args.first < kept_from:
        print(
            f"note: days before {kept_from} are deleted after the retention period "
            f"({settings.active_users_retention_days} days), so this count may be low.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
