"""Daily price ingestion from Alpaca into the `prices` table.

Fetches daily bars for the frozen equity universe (protocol §2, IEX feed, raw
and split-adjusted) and the crypto side universe (§13), and stores them as
versioned rows: a new version of a bar is written only when its values differ
from the latest stored version, so re-runs don't pile up duplicates.

Bars for days that haven't closed yet are never stored (CLAUDE.md rule 1):
  * equity bar for date D: only once D's session has closed, using the real
    close from the cached market calendar (13:00 ET on early-close days);
    16:00 ET for a bar dated on a day with no session
  * crypto bar starting at T (00:00 UTC): only once it's T + 24h

Usage:
    python -m data.ingest --days 5              # recent bars (daily job)
    python -m data.ingest --start 2024-11-01    # backfill from a date

Backfill uses the same fetch, closed-bar, versioning and retry rules. The
window is split into calendar-year chunks (3 API calls each, a short pause
between them), each stored in its own transaction with its own fetched_at,
so a failure keeps the chunks already stored and a re-run is a no-op for them.

After storing, a coverage report lists per symbol the first and last date,
the bar count, a late start, and gaps: missing NYSE sessions (Alpaca trading
calendar) for equities, missing calendar days for crypto. Gaps are reported,
never filled.

Every run writes heartbeat rows (job 'fetch_prices'): started, then finished
(with counts) or error. API keys are read from .env and never printed.

After a successful fetch, the data checks (data/checks.py) run on the same
window and write their own heartbeat rows (job 'data_checks', same run_id).
The exit code is 1 if they find an error; warnings alone leave it at 0.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

from data import calendar as cal
from data import db

JOB = "fetch_prices"
ROOT = Path(__file__).resolve().parents[1]
UNIVERSE_CSV = ROOT / "research" / "universe" / "universe.csv"
CRYPTO_SYMBOLS = ("BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD")  # §13
NY = ZoneInfo("America/New_York")
EQUITY_CLOSE_HOUR = 16  # 16:00 ET: fallback for bars dated on a day with no session

# Waits between attempts: 3 attempts over ~2 minutes.
RETRY_DELAYS = (30, 90)

# Pause between backfill chunks (seconds); keeps us far below 200 calls/min.
CHUNK_PAUSE = 1.0

VALUE_FIELDS = (
    "open", "high", "low", "close", "volume",
    "adj_open", "adj_high", "adj_low", "adj_close", "adj_volume",
    "vwap", "trade_count",
)


class IngestError(Exception):
    """A failure that stops the run (after retries, or a precondition)."""


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------

def is_retryable(exc: BaseException) -> bool:
    """Connection errors, timeouts and HTTP 429/5xx are worth retrying."""
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 429 or 500 <= status <= 599
    return False


def describe(exc: BaseException) -> str:
    """Short, key-free description of an exception (no headers, no request)."""
    status = getattr(exc, "status_code", None)
    msg = str(exc)[:300]
    return f"{type(exc).__name__}" + (f" (HTTP {status})" if status else "") + f": {msg}"


def with_retries(
    fn: Callable[[], Any],
    what: str,
    sleep: Callable[[float], None] = time.sleep,
    delays: tuple[float, ...] = RETRY_DELAYS,
) -> Any:
    """Call fn(); on a retryable error wait and try again, up to len(delays)+1 tries."""
    attempts = len(delays) + 1
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - classified below
            if not is_retryable(exc):
                raise IngestError(f"{what}: {describe(exc)}") from exc
            if i == attempts - 1:
                raise IngestError(f"{what}: gave up after {attempts} attempts: {describe(exc)}") from exc
            print(f"  {what}: attempt {i + 1}/{attempts} failed ({describe(exc)}); "
                  f"retrying in {delays[i]:.0f}s", file=sys.stderr)
            sleep(delays[i])
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Alpaca access (injected so tests can use fakes)
# ---------------------------------------------------------------------------

@dataclass
class Clients:
    trading: Any  # alpaca.trading.client.TradingClient
    stock: Any    # alpaca.data.historical.StockHistoricalDataClient
    crypto: Any   # alpaca.data.historical.CryptoHistoricalDataClient


def make_clients() -> Clients:
    from dotenv import load_dotenv
    from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
    from alpaca.trading.client import TradingClient

    load_dotenv(ROOT / ".env")
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise IngestError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set in .env")
    return Clients(
        trading=TradingClient(key, secret, paper=True),
        stock=StockHistoricalDataClient(key, secret),
        crypto=CryptoHistoricalDataClient(key, secret),
    )


def load_universe(path: Path = UNIVERSE_CSV) -> list[str]:
    with open(path, newline="", encoding="utf-8") as f:
        return [row["symbol"] for row in csv.DictReader(f)]


def bars_by_symbol(response: Any) -> dict[str, list[Any]]:
    """alpaca-py BarSet -> {symbol: [Bar, ...]} (also accepts a plain dict)."""
    data = getattr(response, "data", response)
    return {sym: list(bars) for sym, bars in data.items()}


def check_crypto_assets(clients: Clients, symbols, sleep) -> None:
    """Stop if any crypto pair is missing or not tradable (no substitution)."""
    problems = []
    for sym in symbols:
        try:
            asset = with_retries(lambda: clients.trading.get_asset(sym), f"get_asset {sym}", sleep)
        except IngestError as exc:
            problems.append(f"{sym}: not available ({exc})")
            continue
        status = str(getattr(asset, "status", "")).lower()
        if not getattr(asset, "tradable", False) or "active" not in status:
            problems.append(f"{sym}: tradable={getattr(asset, 'tradable', None)}, status={status}")
    if problems:
        raise IngestError("crypto pair check failed: " + "; ".join(problems))


def fetch_equity(clients: Clients, symbols, start: datetime, end: datetime, sleep):
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    if not symbols:
        return {}, {}
    out = {}
    for adj in (Adjustment.RAW, Adjustment.SPLIT):
        req = StockBarsRequest(symbol_or_symbols=list(symbols), timeframe=TimeFrame.Day,
                               start=start, end=end, feed=DataFeed.IEX, adjustment=adj)
        resp = with_retries(lambda: clients.stock.get_stock_bars(req), f"stock bars ({adj.value})", sleep)
        out[adj.value] = bars_by_symbol(resp)
    return out["raw"], out["split"]


def fetch_crypto(clients: Clients, symbols, start: datetime, end: datetime, sleep):
    from alpaca.data.requests import CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame

    if not symbols:
        return {}
    req = CryptoBarsRequest(symbol_or_symbols=list(symbols), timeframe=TimeFrame.Day,
                            start=start, end=end)
    return bars_by_symbol(with_retries(lambda: clients.crypto.get_crypto_bars(req), "crypto bars", sleep))


# ---------------------------------------------------------------------------
# Bar rules: closed or not, and the stored row
# ---------------------------------------------------------------------------

def as_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("naive bar timestamp")
    return ts.astimezone(timezone.utc)


def equity_bar_date(bar_ts: datetime) -> str:
    return as_utc(bar_ts).astimezone(NY).date().isoformat()


def default_close(d: date) -> datetime:
    """16:00 ET on d: the close when the calendar has no session for d."""
    return datetime(d.year, d.month, d.day, EQUITY_CLOSE_HOUR, tzinfo=NY)


def equity_bar_closed(bar_ts: datetime, now: datetime,
                      close_of: Callable[[date], datetime | None] | None = None) -> bool:
    """True once the session of the bar's date has closed.

    `close_of(d)` gives the session close (None: no session that day, so 16:00 ET).
    Without it, 16:00 ET is used.
    """
    d = as_utc(bar_ts).astimezone(NY).date()
    close = close_of(d) if close_of is not None else None
    return now >= (close or default_close(d))


def calendar_closes(conn: sqlite3.Connection, trading: Any) -> Callable[[date], datetime | None]:
    """close_of(d) from the cached calendar: real (early) closes; None on closed days.

    CalendarUnavailable propagates: we never guess a close.
    """
    def close_of(d: date) -> datetime | None:
        s = cal.session(d, conn, trading)
        return None if s is None else s.close
    return close_of


def crypto_bar_date(bar_ts: datetime) -> str:
    ts = as_utc(bar_ts)
    if (ts.hour, ts.minute, ts.second, ts.microsecond) != (0, 0, 0, 0):
        # Protocol §13 assumes 00:00 UTC daily bars; refuse anything else.
        raise IngestError(f"crypto daily bar not at 00:00 UTC: {ts.isoformat()}")
    return ts.date().isoformat()


def crypto_bar_closed(bar_ts: datetime, now: datetime) -> bool:
    return now >= as_utc(bar_ts) + timedelta(days=1)


def make_row(symbol, asset_class, bar_date, raw, adj, feed) -> dict:
    return {
        "symbol": symbol, "asset_class": asset_class, "bar_date": bar_date,
        "bar_ts": db.utc_ts(as_utc(raw.timestamp)),
        "open": float(raw.open), "high": float(raw.high), "low": float(raw.low),
        "close": float(raw.close), "volume": float(raw.volume),
        "adj_open": float(adj.open), "adj_high": float(adj.high), "adj_low": float(adj.low),
        "adj_close": float(adj.close), "adj_volume": float(adj.volume),
        "vwap": None if raw.vwap is None else float(raw.vwap),
        "trade_count": None if raw.trade_count is None else int(raw.trade_count),
        "feed": feed,
    }


# ---------------------------------------------------------------------------
# Versioned storage
# ---------------------------------------------------------------------------

def latest_version(conn: sqlite3.Connection, symbol: str, bar_date: str):
    return conn.execute(
        "SELECT * FROM prices WHERE symbol = ? AND bar_date = ? "
        "ORDER BY fetched_at DESC LIMIT 1", (symbol, bar_date)).fetchone()


def differs(stored: sqlite3.Row | None, row: dict) -> bool:
    if stored is None:
        return True
    if stored["bar_ts"] != row["bar_ts"] or stored["feed"] != row["feed"]:
        return True
    return any(stored[f] != row[f] for f in VALUE_FIELDS)


def store(conn: sqlite3.Connection, rows: list[dict], fetched_at: str, stats) -> None:
    """Insert new versions only, in one transaction."""
    cols = ("symbol", "asset_class", "bar_date", "bar_ts") + VALUE_FIELDS + ("feed", "fetched_at")
    sql = f"INSERT INTO prices ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    with conn:
        for row in rows:
            s = stats.sym(row["symbol"])
            if differs(latest_version(conn, row["symbol"], row["bar_date"]), row):
                conn.execute(sql, [row[c] for c in cols[:-1]] + [fetched_at])
                s["inserted"] += 1
            else:
                s["unchanged"] += 1


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_ranges(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Split [start, end] into calendar-year chunks (UTC) that never overlap.

    Each chunk ends 1 s before the next one starts; the last ends at `end`.
    Empty if start >= end.
    """
    start, end = as_utc(start), as_utc(end)
    out = []
    cur = start
    while cur < end:
        nxt = datetime(cur.year + 1, 1, 1, tzinfo=timezone.utc)
        out.append((cur, min(nxt - timedelta(seconds=1), end)))
        cur = nxt
    return out


# ---------------------------------------------------------------------------
# Coverage report (gaps are listed, never filled)
# ---------------------------------------------------------------------------

def _as_date(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def expected_equity_days(clients: Clients, start: date, now: datetime, sleep) -> list[str]:
    """NYSE sessions from `start` whose close (16:00 ET) has passed at `now`."""
    from alpaca.trading.requests import GetCalendarRequest

    req = GetCalendarRequest(start=start, end=as_utc(now).astimezone(NY).date())
    sessions = with_retries(lambda: clients.trading.get_calendar(req), "trading calendar", sleep)
    days = []
    for c in sessions:
        d = _as_date(c.date)
        close = cal._alpaca_time(d, c.close) if getattr(c, "close", None) is not None else default_close(d)
        if d >= start and now >= close:
            days.append(d.isoformat())
    return sorted(set(days))


def expected_crypto_days(start: date, now: datetime) -> list[str]:
    """Every calendar day from `start` whose 00:00 UTC bar has closed at `now`."""
    last = as_utc(now).date() - timedelta(days=1)
    n = (last - start).days + 1
    return [(start + timedelta(days=i)).isoformat() for i in range(max(n, 0))]


def date_ranges(dates: list[str], expected: list[str]) -> list[str]:
    """Group dates that are consecutive in `expected` into 'a..b' ranges."""
    pos = {d: i for i, d in enumerate(expected)}
    out: list[list[str]] = []
    prev = None
    for d in sorted(dates, key=lambda x: pos.get(x, -1)):
        i = pos.get(d)
        if out and i is not None and prev is not None and i == prev + 1:
            out[-1][1] = d
        else:
            out.append([d, d])
        prev = i
    return [a if a == b else f"{a}..{b}" for a, b in out]


def symbol_coverage(stored: list[str], expected: list[str]) -> dict:
    """Compare stored bar dates with expected ones for one symbol."""
    stored = sorted(set(stored))
    have, exp = set(stored), set(expected)
    first = stored[0] if stored else None
    missing = sorted(exp - have)
    before = [d for d in missing if first is None or d < first]
    gaps = [d for d in missing if first is not None and d > first]
    return {
        "first": first,
        "last": stored[-1] if stored else None,
        "bars": len(stored),
        "expected": len(expected),
        "late_start": bool(before),
        "missing_before_first": len(before),
        "gaps": gaps,
        "gap_ranges": date_ranges(gaps, expected),
        "unexpected": sorted(have - exp),  # bars on days we didn't expect (not filled or dropped)
    }


def stored_dates(conn: sqlite3.Connection, symbol: str, start: date) -> list[str]:
    rows = conn.execute("SELECT DISTINCT bar_date FROM prices WHERE symbol = ? AND bar_date >= ? "
                        "ORDER BY bar_date", (symbol, start.isoformat()))
    return [r[0] for r in rows]


def coverage_report(conn, clients: Clients, equity_symbols, crypto_symbols,
                    start: date, now: datetime, sleep) -> dict[str, dict]:
    eq_days = expected_equity_days(clients, start, now, sleep) if equity_symbols else []
    cr_days = expected_crypto_days(start, now)
    report = {}
    for sym in equity_symbols:
        report[sym] = {"asset_class": "us_equity", **symbol_coverage(stored_dates(conn, sym, start), eq_days)}
    for sym in crypto_symbols:
        report[sym] = {"asset_class": "crypto", **symbol_coverage(stored_dates(conn, sym, start), cr_days)}
    return report


def flagged(report: dict[str, dict]) -> dict[str, dict]:
    return {s: r for s, r in report.items() if r["late_start"] or r["gaps"] or r["unexpected"]}


def format_report(report: dict[str, dict], start: date) -> str:
    lines = [f"Coverage from {start.isoformat()}:",
             f"{'symbol':<10} {'first':<10}  {'last':<10}  {'bars':>5} {'expected':>8}  late  gaps"]
    for sym, r in report.items():
        lines.append(f"{sym:<10} {r['first'] or '-':<10}  {r['last'] or '-':<10}  {r['bars']:>5} "
                     f"{r['expected']:>8}  {'yes' if r['late_start'] else 'no':<4}  {len(r['gaps'])}")
    bad = flagged(report)
    if not bad:
        lines.append("No late starts, gaps or unexpected days.")
    for sym, r in bad.items():
        if r["late_start"]:
            lines.append(f"  {sym}: starts {r['first'] or 'never'}, "
                         f"{r['missing_before_first']} expected days before its first bar")
        if r["gaps"]:
            lines.append(f"  {sym}: {len(r['gaps'])} missing days: {', '.join(r['gap_ranges'])}")
        if r["unexpected"]:
            lines.append(f"  {sym}: bars on unexpected days: {', '.join(r['unexpected'])}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

@dataclass
class Stats:
    per_symbol: dict[str, dict[str, int]] = field(default_factory=dict)

    def sym(self, symbol: str) -> dict[str, int]:
        return self.per_symbol.setdefault(
            symbol, {"fetched": 0, "inserted": 0, "unchanged": 0, "rejected_unclosed": 0})

    def totals(self) -> dict[str, int]:
        keys = ("fetched", "inserted", "unchanged", "rejected_unclosed")
        return {k: sum(s[k] for s in self.per_symbol.values()) for k in keys}


def heartbeat(conn, run_id: str, event: str, detail: dict) -> None:
    with conn:
        conn.execute("INSERT INTO heartbeat (run_id, job, event, at, detail) VALUES (?, ?, ?, ?, ?)",
                     (run_id, JOB, event, db.utc_ts(), json.dumps(detail, sort_keys=True)))


def build_rows(equity_raw, equity_split, crypto, now: datetime, stats: Stats,
               close_of: Callable[[date], datetime | None] | None = None) -> list[dict]:
    rows = []
    for sym in sorted(set(equity_raw) | set(equity_split)):
        raw = {equity_bar_date(b.timestamp): b for b in equity_raw.get(sym, [])}
        adj = {equity_bar_date(b.timestamp): b for b in equity_split.get(sym, [])}
        if raw.keys() != adj.keys():
            raise IngestError(f"{sym}: raw and split-adjusted fetches cover different days: "
                              f"{sorted(raw.keys() ^ adj.keys())}")
        for d in sorted(raw):
            s = stats.sym(sym)
            s["fetched"] += 1
            if not equity_bar_closed(raw[d].timestamp, now, close_of):
                s["rejected_unclosed"] += 1
                continue
            rows.append(make_row(sym, "us_equity", d, raw[d], adj[d], "iex"))
    for sym in sorted(crypto):
        for b in crypto[sym]:
            s = stats.sym(sym)
            s["fetched"] += 1
            d = crypto_bar_date(b.timestamp)
            if not crypto_bar_closed(b.timestamp, now):
                s["rejected_unclosed"] += 1
                continue
            rows.append(make_row(sym, "crypto", d, b, b, "crypto"))  # no adjustment for crypto
    return rows


def first_missing_day(conn: sqlite3.Connection, symbols: list[str]) -> datetime | None:
    """00:00 UTC of the day after the oldest of each symbol's last stored bar; None if none stored.

    A recent run that would start after this would leave a hole, so it starts here minus its
    usual overlap instead. Symbols with no bar at all are left to a backfill (start_date).
    """
    if not symbols:
        return None
    names = ", ".join("?" * len(symbols))
    rows = conn.execute(f"SELECT symbol, MAX(bar_date) FROM prices WHERE symbol IN ({names}) GROUP BY symbol",
                        list(symbols)).fetchall()
    if not rows:
        return None
    d = min(date.fromisoformat(r[1]) for r in rows) + timedelta(days=1)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def run(conn: sqlite3.Connection, clients: Clients, days: int = 5,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
        equity_symbols: list[str] | None = None,
        crypto_symbols=CRYPTO_SYMBOLS,
        start_date: date | None = None,
        chunk_pause: float = CHUNK_PAUSE,
        data_checks: bool = True,
        run_id: str | None = None) -> int:
    """One ingestion run. Returns a process exit code (0 ok, 1 error).

    With `start_date`, backfill from that date (00:00 UTC) instead of `days`. Without it, the run
    starts `days` back; if a symbol's first missing day (the day after its last stored bar) is
    older than that, it starts there minus the same `days` of overlap, so an outage leaves no holes.
    Rule 1 is unaffected: every row's fetched_at is the real fetch time.
    With `data_checks`, the checks run afterwards and an error in them gives 1.
    run_id: the caller's (e.g. the daily runner's), so all its heartbeat rows share one.
    """
    run_id = run_id or uuid.uuid4().hex
    equity_symbols = equity_symbols if equity_symbols is not None else load_universe()
    start_now = now()
    resume_from = None
    if start_date is None:
        start = (start_now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        first_missing = first_missing_day(conn, list(equity_symbols) + list(crypto_symbols))
        if first_missing is not None and first_missing < start:
            # a long outage: back to the first missing day, minus the usual overlap, so no holes
            resume_from = first_missing - timedelta(days=days)
            start = resume_from
    else:
        start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc)
    chunks = chunk_ranges(start, start_now)
    heartbeat(conn, run_id, "started", {
        "mode": "recent" if start_date is None else "backfill",
        "days": days if start_date is None else None, "start": db.utc_ts(start),
        "resume_from": None if resume_from is None else db.utc_ts(resume_from),
        "chunks": [[db.utc_ts(a), db.utc_ts(b)] for a, b in chunks],
        "equity": len(equity_symbols), "crypto": len(crypto_symbols)})
    stats = Stats()
    fetched_ats: list[str] = []
    try:
        if not chunks:
            raise IngestError(f"start {db.utc_ts(start)} is not before now")
        check_crypto_assets(clients, crypto_symbols, sleep)
        close_of = calendar_closes(conn, clients.trading)
        for i, (a, b) in enumerate(chunks):
            if i:
                sleep(chunk_pause)
            eq_raw, eq_split = fetch_equity(clients, equity_symbols, a, b, sleep)
            cr = fetch_crypto(clients, crypto_symbols, a, b, sleep)
            rows = build_rows(eq_raw, eq_split, cr, now(), stats, close_of)
            fetched_at = db.utc_ts(now())
            store(conn, rows, fetched_at, stats)
            fetched_ats.append(fetched_at)
            if len(chunks) > 1:
                print(f"  chunk {i + 1}/{len(chunks)} {a.date()}..{b.date()}: {len(rows)} closed bars")
        missing = [s for s in list(equity_symbols) + list(crypto_symbols) if s not in stats.per_symbol]
        report = coverage_report(conn, clients, equity_symbols, crypto_symbols, start.date(), now(), sleep)
    except Exception as exc:  # noqa: BLE001 - every failure becomes an error heartbeat
        err = str(exc) if isinstance(exc, IngestError) else describe(exc)
        heartbeat(conn, run_id, "error", {"error": err, "counts": stats.totals(),
                                          "chunks_stored": len(fetched_ats)})
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    coverage = {s: {k: r[k] for k in ("first", "last", "bars", "expected", "late_start", "gap_ranges",
                                      "unexpected")} for s, r in report.items()}
    heartbeat(conn, run_id, "finished", {"fetched_at": fetched_at, "fetched_at_by_chunk": fetched_ats,
                                         "totals": stats.totals(), "per_symbol": stats.per_symbol,
                                         "no_bars": missing, "coverage": coverage,
                                         "flagged": sorted(flagged(report))})
    t = stats.totals()
    print(f"run {run_id}: fetched {t['fetched']}, inserted {t['inserted']}, "
          f"unchanged {t['unchanged']}, rejected (unclosed) {t['rejected_unclosed']}"
          + (f"; no bars for {missing}" if missing else ""))
    print(format_report(report, start.date()))
    if not data_checks:
        return 0
    from data import checks  # imports this module, so not at the top
    try:
        code, problems, splits = checks.check_and_alert(conn, run_id, equity_symbols, crypto_symbols,
                                                        start.date(), now(), clients.trading)
    except Exception as exc:  # noqa: BLE001 - a crash in the checks is an error too
        checks._heartbeat(conn, run_id, "error", {"status": "error", "error": describe(exc)})
        print(f"ERROR: data checks failed: {describe(exc)}", file=sys.stderr)
        return 1
    print("Data checks:\n" + checks.format_report(problems, splits))
    return code


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fetch daily bars from Alpaca into the prices table.")
    when = p.add_mutually_exclusive_group()
    when.add_argument("--days", type=int, default=None, help="look back N calendar days (default 5)")
    when.add_argument("--start", type=date.fromisoformat, default=None, metavar="YYYY-MM-DD",
                      help="backfill from this date to the latest closed bar")
    p.add_argument("--db", default=str(db.DEFAULT_DB_PATH), help="database path")
    args = p.parse_args(argv)
    if args.days is None:
        args.days = 5
    if args.days < 1:
        p.error("--days must be >= 1")
    if args.start is not None and args.start >= datetime.now(timezone.utc).date():
        p.error("--start must be before today (UTC)")
    conn = db.init_db(args.db)
    try:
        try:
            clients = make_clients()
        except IngestError as exc:
            heartbeat(conn, uuid.uuid4().hex, "error", {"error": str(exc)})
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        return run(conn, clients, days=args.days, start_date=args.start)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
