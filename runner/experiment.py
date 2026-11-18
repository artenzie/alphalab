"""Experiment databases (protocol §9, v1.13): one SQLite file per experiment.

    python -m runner.experiment create shakedown --start 2026-12-14 --from data/alphalab.db
    python -m runner.experiment create official --start 2027-01-11 --from data/shakedown.db
    python -m runner.experiment show --experiment official

A new experiment file gets the schema, its label (the single `experiment`
row: name, start date, source) and a copy of the market data from an existing
database: prices, the calendar cache and the crypto fill bars, with their ids
and fetched_at unchanged, so point-in-time reads give the same answers. It
gets no ledger rows, so every agent starts at START_CASH ($100,000) on the
start date. The source is opened read-only and never changed; an existing
output file is never overwritten.

Sun 10 Jan 2027 reset: create `official` from the shakedown file, then point
the scheduled runs at --experiment official. The shakedown file is kept as is.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from data import db

EXPERIMENTS_DIR = db.DATA_DIR
OFFICIAL = "official"
SHAKEDOWN = "shakedown"
OFFICIAL_START = date(2027, 1, 11)          # D3
SHAKEDOWN_START = date(2026, 12, 14)
MARKET_TABLES = ("calendar_fetches", "market_sessions", "prices", "crypto_fill_bars")   # FK order


class ExperimentError(Exception):
    """The experiment database is missing, unlabelled, or doesn't fit the request."""


@dataclass(frozen=True)
class Experiment:
    name: str
    start_date: date
    source: str | None = None


def path_for(name: str) -> Path:
    """data/<name>.db for a named experiment (shakedown, official, ...)."""
    return EXPERIMENTS_DIR / f"{name}.db"


def get(conn: sqlite3.Connection) -> Experiment | None:
    row = conn.execute("SELECT name, start_date, source FROM experiment ORDER BY experiment_id LIMIT 1").fetchone()
    if row is None:
        return None
    return Experiment(row["name"], date.fromisoformat(row["start_date"]), row["source"])


def require(conn: sqlite3.Connection, day: date | None = None) -> Experiment:
    """The label, checked: present, official never before 11 Jan, day not before the start."""
    exp = get(conn)
    if exp is None:
        raise ExperimentError("this database has no experiment label; create it with "
                              "`python -m runner.experiment create`")
    if exp.name == OFFICIAL and exp.start_date != OFFICIAL_START:
        raise ExperimentError(f"the official experiment must start on {OFFICIAL_START}, not {exp.start_date}")
    if day is not None and day < exp.start_date:
        raise ExperimentError(f"{day} is before the {exp.name} experiment's start date {exp.start_date}")
    return exp


def create(name: str, start: date, source: Path, out: Path) -> Experiment:
    """A new labelled experiment database at `out` with the market data of `source`."""
    if name == OFFICIAL and start != OFFICIAL_START:
        raise ExperimentError(f"the official experiment starts on {OFFICIAL_START} (D3), not {start}")
    if out.exists():
        raise ExperimentError(f"{out} already exists; an experiment database is never overwritten")
    if not source.exists():
        raise ExperimentError(f"source database {source} not found")
    if source.resolve() == out.resolve():
        raise ExperimentError("source and output are the same file")
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    conn = db.init_db(out)
    try:
        if get(conn) is not None:
            raise ExperimentError(f"{out} already has an experiment label")
        with conn:
            for table in MARKET_TABLES:
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
                src_cols = {r[1] for r in src.execute(f"PRAGMA table_info({table})")}
                missing = [c for c in cols if c not in src_cols]
                if missing:
                    raise ExperimentError(f"source {table} lacks columns {missing}")
                names = ", ".join(cols)
                rows = src.execute(f"SELECT {names} FROM {table} ORDER BY rowid").fetchall()
                conn.executemany(f"INSERT INTO {table} ({names}) VALUES ({', '.join('?' * len(cols))})",
                                 [tuple(r) for r in rows])
            conn.execute("INSERT INTO experiment (name, start_date, source) VALUES (?, ?, ?)",
                         (name, start.isoformat(), source.name))
        return require(conn)
    except BaseException:
        conn.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(out) + suffix).unlink(missing_ok=True)       # our own half-made file, never a source
        raise
    finally:
        conn.close()                                              # closing twice is harmless
        src.close()


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in db.ALL_TABLES}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Create or show an experiment database (protocol §9, v1.13).")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="new labelled database with a copy of the market data")
    c.add_argument("name")
    c.add_argument("--start", type=date.fromisoformat, required=True, metavar="YYYY-MM-DD")
    c.add_argument("--from", dest="source", type=Path, default=db.DEFAULT_DB_PATH, help="source database")
    c.add_argument("--db", type=Path, default=None, help="output path (default data/<name>.db)")
    s = sub.add_parser("show", help="print the label and row counts")
    where = s.add_mutually_exclusive_group(required=True)
    where.add_argument("--experiment")
    where.add_argument("--db", type=Path)
    args = p.parse_args(argv)
    try:
        if args.cmd == "create":
            out = args.db or path_for(args.name)
            exp = create(args.name, args.start, args.source, out)
            print(f"created {out}: experiment {exp.name!r}, start {exp.start_date}, market data from {exp.source}")
            conn = db.connect(out)
        else:
            path = args.db or path_for(args.experiment)
            if not path.exists():
                raise ExperimentError(f"{path} not found")
            conn = db.connect(path)
            exp = require(conn)
            print(f"{path}: experiment {exp.name!r}, start {exp.start_date}, market data from {exp.source}")
        try:
            for t, n in counts(conn).items():
                print(f"  {t:<18} {n:>8}")
        finally:
            conn.close()
    except ExperimentError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
