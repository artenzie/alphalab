"""AlphaLab database: create the schema and hand out connections.

No trading logic lives here. Usage:

    from data import db
    conn = db.init_db()          # creates data/alphalab.db if needed
    conn = db.connect()          # existing database

    python -m data.db            # create/upgrade the database from the shell
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = DATA_DIR / "alphalab.db"
SCHEMA_PATH = DATA_DIR / "schema.sql"

# Every data table except the editable `agents` reference table.
APPEND_ONLY_TABLES = (
    "prices",
    "snapshots",
    "decisions",
    "risk_events",
    "orders",
    "fills",
    "positions",
    "equity",
    "mirror_recon",
    "heartbeat",
    "calendar_fetches",
    "market_sessions",
)
ALL_TABLES = ("agents",) + APPEND_ONLY_TABLES

# Columns added after a table was first created. schema.sql has them too (as the
# table's last columns), but its CREATE TABLE IF NOT EXISTS leaves an existing
# table alone, so init_db adds any that are missing. ADD COLUMN doesn't rewrite
# rows and fires no UPDATE trigger; existing rows read NULL ("not recorded").
COLUMN_UPGRADES = (
    ("decisions", "reasoning_tokens",
     "INTEGER CHECK (reasoning_tokens IS NULL OR (reasoning_tokens >= 0 AND "
     "(tokens_out IS NULL OR reasoning_tokens <= tokens_out)))"),
)


def utc_ts(dt: datetime | None = None) -> str:
    """Format a timestamp the way the schema requires: 2026-11-16T22:45:00.123Z.

    With no argument, returns the current UTC time. Naive datetimes are
    rejected, because their time zone is ambiguous.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive datetime: attach a time zone before storing it")
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def connect(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a connection with foreign keys on, WAL journaling and dict-like rows."""
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Create (or bring up to date) the schema and return a connection.

    Safe to call repeatedly: the schema only uses IF NOT EXISTS / INSERT OR IGNORE,
    and COLUMN_UPGRADES only adds columns that are missing.
    """
    conn = connect(path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute("PRAGMA foreign_keys = ON")  # executescript commits; keep FKs on
    with conn:
        for table, column, definition in COLUMN_UPGRADES:
            if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    return conn


def prices_as_of(
    conn: sqlite3.Connection,
    cutoff_ts: str,
    symbols: list[str] | None = None,
) -> list[sqlite3.Row]:
    """Point-in-time read of daily bars.

    Returns, for every (symbol, bar_date) with bar_ts <= cutoff_ts, the version
    with the latest fetched_at that is still <= cutoff_ts. Versions fetched
    after the cutoff (e.g. a later split re-adjustment) are invisible, so a
    snapshot built from this can only see what was known at cutoff_ts.
    """
    sql = """
        SELECT p.*
        FROM prices AS p
        WHERE p.bar_ts <= :cutoff
          AND p.fetched_at = (
                SELECT MAX(q.fetched_at)
                FROM prices AS q
                WHERE q.symbol = p.symbol
                  AND q.bar_date = p.bar_date
                  AND q.fetched_at <= :cutoff
          )
    """
    params: dict[str, object] = {"cutoff": cutoff_ts}
    if symbols:
        names = [f":s{i}" for i in range(len(symbols))]
        sql += f" AND p.symbol IN ({', '.join(names)})"
        params.update({f"s{i}": s for i, s in enumerate(symbols)})
    sql += " ORDER BY p.symbol, p.bar_date"
    return conn.execute(sql, params).fetchall()


if __name__ == "__main__":
    c = init_db()
    n = c.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    print(f"Database ready at {DEFAULT_DB_PATH} ({n} agents).")
    c.close()
