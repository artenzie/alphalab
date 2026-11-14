"""Daily price ingestion from Alpaca into the `prices` table.

Fetches daily bars for the frozen equity universe (protocol §2, IEX feed, raw
and split-adjusted) and the crypto side universe (§13), and stores them as
versioned rows: a new version of a bar is written only when its values differ
from the latest stored version, so re-runs don't pile up duplicates.

Bars for days that haven't closed yet are never stored (CLAUDE.md rule 1):
  * equity bar for date D: only once it's 16:00 America/New_York on D
  * crypto bar starting at T (00:00 UTC): only once it's T + 24h

Usage:
    python -m data.ingest --days 5

Every run writes heartbeat rows (job 'fetch_prices'): started, then finished
(with counts) or error. API keys are read from .env and never printed.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

from data import db

JOB = "fetch_prices"
ROOT = Path(__file__).resolve().parents[1]
UNIVERSE_CSV = ROOT / "research" / "universe" / "universe.csv"
CRYPTO_SYMBOLS = ("BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD")  # §13
NY = ZoneInfo("America/New_York")
EQUITY_CLOSE_HOUR = 16  # 16:00 ET; conservative on early-close days

# Waits between attempts: 3 attempts over ~2 minutes.
RETRY_DELAYS = (30, 90)

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


def equity_bar_closed(bar_ts: datetime, now: datetime) -> bool:
    d = as_utc(bar_ts).astimezone(NY).date()
    close = datetime(d.year, d.month, d.day, EQUITY_CLOSE_HOUR, tzinfo=NY)
    return now >= close


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


def build_rows(equity_raw, equity_split, crypto, now: datetime, stats: Stats) -> list[dict]:
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
            if not equity_bar_closed(raw[d].timestamp, now):
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


def run(conn: sqlite3.Connection, clients: Clients, days: int = 5,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
        equity_symbols: list[str] | None = None,
        crypto_symbols=CRYPTO_SYMBOLS) -> int:
    """One ingestion run. Returns a process exit code (0 ok, 1 error)."""
    run_id = uuid.uuid4().hex
    equity_symbols = equity_symbols if equity_symbols is not None else load_universe()
    start_now = now()
    start = (start_now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    heartbeat(conn, run_id, "started", {"days": days, "start": db.utc_ts(start),
                                        "equity": len(equity_symbols), "crypto": len(crypto_symbols)})
    stats = Stats()
    try:
        check_crypto_assets(clients, crypto_symbols, sleep)
        eq_raw, eq_split = fetch_equity(clients, equity_symbols, start, start_now, sleep)
        cr = fetch_crypto(clients, crypto_symbols, start, start_now, sleep)
        rows = build_rows(eq_raw, eq_split, cr, now(), stats)
        fetched_at = db.utc_ts(now())
        store(conn, rows, fetched_at, stats)
        missing = [s for s in list(equity_symbols) + list(crypto_symbols) if s not in stats.per_symbol]
    except Exception as exc:  # noqa: BLE001 - every failure becomes an error heartbeat
        err = str(exc) if isinstance(exc, IngestError) else describe(exc)
        heartbeat(conn, run_id, "error", {"error": err, "counts": stats.totals()})
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    heartbeat(conn, run_id, "finished", {"fetched_at": fetched_at, "totals": stats.totals(),
                                         "per_symbol": stats.per_symbol, "no_bars": missing})
    t = stats.totals()
    print(f"run {run_id}: fetched {t['fetched']}, inserted {t['inserted']}, "
          f"unchanged {t['unchanged']}, rejected (unclosed) {t['rejected_unclosed']}"
          + (f"; no bars for {missing}" if missing else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fetch daily bars from Alpaca into the prices table.")
    p.add_argument("--days", type=int, default=5, help="look back N calendar days (default 5)")
    p.add_argument("--db", default=str(db.DEFAULT_DB_PATH), help="database path")
    args = p.parse_args(argv)
    if args.days < 1:
        p.error("--days must be >= 1")
    conn = db.init_db(args.db)
    try:
        try:
            clients = make_clients()
        except IngestError as exc:
            heartbeat(conn, uuid.uuid4().hex, "error", {"error": str(exc)})
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        return run(conn, clients, days=args.days)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
