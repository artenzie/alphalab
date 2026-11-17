"""Crypto fill prices (protocol §7 rule 4, v1.12): closed 1-minute bars after each crypto decision.

For a crypto decision on the daily bar of D (decided at 00:15Z on D+1, §13),
the ledger fills at the open of the first 1-minute bar starting at or after
the decision time. This module fetches the 1-minute bars of the five coins in
the window [00:15Z, 01:15Z) on D+1 and stores them in `crypto_fill_bars`, a
table of its own: no snapshot reads it, so no agent ever sees these prices.

Rules, the same as the daily ingest's:
  * only closed bars: a minute bar starting at T is stored once now >= T + 1 min
    (CLAUDE.md rule 1; the schema rejects a row fetched before its minute ended)
  * versioned: a bar is inserted only when new or different from the latest
    stored version of (symbol, bar_ts), so re-runs add nothing
  * a bar that isn't a sane OHLC bar fails the run; nothing is stored for it
  * heartbeat rows (job 'fetch_fill_prices'): started, then finished or error.
    'finished' warns for a coin with no bar yet, or whose first bar starts more
    than LATE_FIRST_BAR after the window start

Usage:
    python -m data.fill_prices --date 2026-11-16     # the window after decision 16 Nov (00:15Z 17 Nov)
    python -m data.fill_prices --start 2026-11-07    # backfill through the last window that has started

The daily job runs from 00:20Z (the 00:15 bar has closed at 00:16Z); a re-run
later in the hour picks up the rest of the window. API keys are read from .env
by data.ingest.make_clients and never printed.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from data import calendar as cal
from data import db
from data.ingest import (CRYPTO_SYMBOLS, Clients, IngestError, as_utc, bars_by_symbol, describe,
                         make_clients, with_retries)

JOB = "fetch_fill_prices"
WINDOW = timedelta(minutes=60)
MINUTE = timedelta(minutes=1)
LATE_FIRST_BAR = timedelta(minutes=5)
VALUE_FIELDS = ("open", "high", "low", "close", "volume", "vwap", "trade_count")
FEED = "crypto"


def window(decision_date: date) -> tuple[datetime, datetime]:
    """[start, end) of the minute bars after decision_date's decision: 00:15Z .. 01:15Z on D+1."""
    start = cal.crypto_times(decision_date).decide_at
    return start, start + WINDOW


def minute_bar_closed(bar_ts: datetime, now: datetime) -> bool:
    return now >= as_utc(bar_ts) + MINUTE


def fetch_minutes(clients: Clients, symbols, start: datetime, end: datetime, sleep) -> dict[str, list[Any]]:
    """Minute bars in [start, end) (Alpaca's end is inclusive, so ask up to end - 1 min)."""
    from alpaca.data.requests import CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame

    req = CryptoBarsRequest(symbol_or_symbols=list(symbols), timeframe=TimeFrame.Minute,
                            start=start, end=end - MINUTE)
    return bars_by_symbol(with_retries(lambda: clients.crypto.get_crypto_bars(req), "crypto minute bars", sleep))


def faults(row: dict) -> list[str]:
    o, h, l, c, v = (row[k] for k in ("open", "high", "low", "close", "volume"))
    if not all(isinstance(x, float) and math.isfinite(x) and x > 0 for x in (o, h, l, c)):
        return [f"OHLC not all positive numbers: {o}, {h}, {l}, {c}"]
    out = []
    if not l <= o <= h:
        out.append(f"open {o} outside low..high {l}..{h}")
    if not l <= c <= h:
        out.append(f"close {c} outside low..high {l}..{h}")
    if not (math.isfinite(v) and v >= 0):
        out.append(f"volume {v} is negative or not a number")
    return out


def build_rows(bars: dict[str, list[Any]], decision_date: date, now: datetime, stats: dict) -> list[dict]:
    """Closed bars inside the window, as crypto_fill_bars rows. Raises IngestError on a bad bar."""
    start, end = window(decision_date)
    rows = []
    for sym in sorted(bars):
        s = stats.setdefault(sym, {"fetched": 0, "inserted": 0, "unchanged": 0, "rejected_unclosed": 0,
                                   "outside_window": 0})
        for b in bars[sym]:
            ts = as_utc(b.timestamp)
            s["fetched"] += 1
            if not start <= ts < end:
                s["outside_window"] += 1
                continue
            if not minute_bar_closed(ts, now):
                s["rejected_unclosed"] += 1
                continue
            row = {"symbol": sym, "decision_date": decision_date.isoformat(), "window_start": db.utc_ts(start),
                   "bar_ts": db.utc_ts(ts), "open": float(b.open), "high": float(b.high), "low": float(b.low),
                   "close": float(b.close), "volume": float(b.volume),
                   "vwap": None if b.vwap is None else float(b.vwap),
                   "trade_count": None if b.trade_count is None else int(b.trade_count), "feed": FEED}
            bad = faults(row)
            if bad:
                raise IngestError(f"{sym} minute bar {row['bar_ts']}: " + "; ".join(bad))
            rows.append(row)
    return rows


def _latest(conn: sqlite3.Connection, symbol: str, bar_ts: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM crypto_fill_bars WHERE symbol = ? AND bar_ts = ? "
                        "ORDER BY fetched_at DESC, fill_bar_id DESC LIMIT 1", (symbol, bar_ts)).fetchone()


def store(conn: sqlite3.Connection, rows: list[dict], fetched_at: str, stats: dict) -> int:
    """Insert new or changed bars only, in one transaction. Returns the number inserted."""
    cols = ("symbol", "decision_date", "window_start", "bar_ts") + VALUE_FIELDS + ("feed", "fetched_at")
    sql = f"INSERT INTO crypto_fill_bars ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    n = 0
    with conn:
        for row in rows:
            old = _latest(conn, row["symbol"], row["bar_ts"])
            if old is not None and old["feed"] == row["feed"] and all(old[f] == row[f] for f in VALUE_FIELDS):
                stats[row["symbol"]]["unchanged"] += 1
                continue
            conn.execute(sql, [row[c] for c in cols[:-1]] + [fetched_at])
            stats[row["symbol"]]["inserted"] += 1
            n += 1
    return n


def first_bars(conn: sqlite3.Connection, decision_date: date, symbols) -> dict[str, str | None]:
    start, end = window(decision_date)
    out = {}
    for sym in symbols:
        r = conn.execute("SELECT MIN(bar_ts) FROM crypto_fill_bars WHERE symbol = ? AND bar_ts >= ? AND bar_ts < ?",
                         (sym, db.utc_ts(start), db.utc_ts(end))).fetchone()
        out[sym] = r[0]
    return out


def warnings_for(conn, decision_date: date, symbols) -> list[str]:
    start, _ = window(decision_date)
    out = []
    for sym, first in first_bars(conn, decision_date, symbols).items():
        if first is None:
            out.append(f"{sym} {decision_date}: no minute bar stored yet")
        elif cal._parse_ts(first) - start > LATE_FIRST_BAR:
            out.append(f"{sym} {decision_date}: first bar {first} is more than "
                       f"{LATE_FIRST_BAR.seconds // 60} min after {db.utc_ts(start)}")
    return out


def _heartbeat(conn, run_id: str, event: str, detail: dict) -> None:
    with conn:
        conn.execute("INSERT INTO heartbeat (run_id, job, event, at, detail) VALUES (?, ?, ?, ?, ?)",
                     (run_id, JOB, event, db.utc_ts(), json.dumps(detail, sort_keys=True)))


def run(conn: sqlite3.Connection, clients: Clients, decision_dates: Iterable[date],
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep, symbols=CRYPTO_SYMBOLS) -> int:
    """Fetch and store the windows after decision_dates. Returns an exit code (0 ok, 1 error)."""
    run_id = uuid.uuid4().hex
    dates = sorted(set(decision_dates))
    _heartbeat(conn, run_id, "started", {"dates": [d.isoformat() for d in dates], "symbols": list(symbols)})
    stats: dict[str, dict] = {}
    per_date, warnings = {}, []
    try:
        if not dates:
            raise IngestError("no decision dates given")
        for d in dates:
            start, end = window(d)
            if now() < start + MINUTE:
                raise IngestError(f"{d}: no minute of the window {db.utc_ts(start)} has closed yet")
            bars = fetch_minutes(clients, symbols, start, end, sleep)
            day_stats: dict[str, dict] = {}
            rows = build_rows(bars, d, now(), day_stats)
            n = store(conn, rows, db.utc_ts(now()), day_stats)
            for sym, s in day_stats.items():
                tot = stats.setdefault(sym, {k: 0 for k in s})
                for k, v in s.items():
                    tot[k] += v
            per_date[d.isoformat()] = {"closed_bars": len(rows), "inserted": n}
            warnings += warnings_for(conn, d, symbols)
    except Exception as exc:  # noqa: BLE001 - every failure becomes an error heartbeat
        err = str(exc) if isinstance(exc, IngestError) else describe(exc)
        _heartbeat(conn, run_id, "error", {"error": err, "per_date": per_date})
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    _heartbeat(conn, run_id, "finished", {"per_date": per_date, "per_symbol": stats, "warnings": warnings})
    ins = sum(v["inserted"] for v in per_date.values())
    print(f"run {run_id}: {len(dates)} window(s), {sum(v['closed_bars'] for v in per_date.values())} closed bars, "
          f"{ins} inserted")
    for w in warnings:
        print(f"  warning: {w}")
    return 0


def last_started_date(now: datetime) -> date:
    """The latest decision date whose window has a closed minute at now."""
    d = as_utc(now).date() - timedelta(days=1)
    while now < window(d)[0] + MINUTE:
        d -= timedelta(days=1)
    return d


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fetch crypto 1-minute fill bars into crypto_fill_bars.")
    when = p.add_mutually_exclusive_group(required=True)
    when.add_argument("--date", type=date.fromisoformat, metavar="YYYY-MM-DD", help="decision date")
    when.add_argument("--start", type=date.fromisoformat, metavar="YYYY-MM-DD",
                      help="backfill decision dates from this one through the last started window")
    p.add_argument("--db", default=str(db.DEFAULT_DB_PATH), help="database path")
    args = p.parse_args(argv)
    now = datetime.now(timezone.utc)
    if args.date is not None:
        dates = [args.date]
    else:
        last = last_started_date(now)
        if args.start > last:
            p.error(f"--start must be on or before {last}")
        dates = [args.start + timedelta(days=i) for i in range((last - args.start).days + 1)]
    conn = db.init_db(args.db)
    try:
        try:
            clients = make_clients()
        except IngestError as exc:
            _heartbeat(conn, uuid.uuid4().hex, "error", {"error": str(exc)})
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        return run(conn, clients, dates)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
