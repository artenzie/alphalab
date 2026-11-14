"""The daily snapshot (protocol §4, §13): the only market data an agent ever sees.

    from data import snapshot
    snap = snapshot.build_snapshot(conn, "us_equity", date(2026, 11, 16), "live")
    snap.payload["symbols"]["AAPL"]["ret_21"]
    snap.history("AAPL", 252)        # rule-based agents: up to 252 adjusted bars
    snapshot.store_snapshot(conn, snap)   # live only: hash + data checks -> snapshots

No future data (CLAUDE.md rule 1). The cutoff is the protocol's snapshot time
for the decision date D: the session close + 1:45 for equities (§3, so
early closes are handled), or 00:10Z on D+1 for crypto (§13). A bar is used
only if all of these hold:
  * bar_date <= D
  * its session has closed by the cutoff (calendar close for equities,
    bar_ts + 24h for crypto)
  * live mode: it is the latest version with fetched_at <= cutoff (db.prices_as_of)

Two modes:
  live      the rules above. This is the only mode the daily run uses and the
            only mode that can be stored.
  backtest  the same rules, but takes the latest stored version of each bar
            whatever its fetched_at. The price backfill was fetched in Nov 2026,
            so a live snapshot for an earlier date would see nothing. A backtest
            snapshot is therefore a reconstruction with today's data and today's
            split adjustment, not what was known on D. It is labelled
            mode='backtest' in its payload and can't be stored.

Fields per symbol, from split-adjusted closes and volumes (null when history
is too short):
  last_date, last_close, dates_20, closes_20 (oldest first)
  ret_1/5/21/63     c[t] / c[t-N] - 1, N bars back
  ma_20, ma_100     simple mean of the last N closes
  vol_20            sample std (ddof=1) of the last 20 daily simple returns,
                    times sqrt(252) (equity) or sqrt(365) (crypto)
  z_20              (c[t] - ma_20) / sample std of the last 20 closes
  volume_ratio_20   today's volume / mean of the last 20 volumes (today included)
  n_bars            history bars available (at most 252)

The history agents get through `history()` is the data loaded at build time,
bounded by the same cutoff; the snapshot holds no database connection. Its
hash (history_sha256) is part of the payload, so the snapshot's sha256 covers
it. The payload has no build time, so the same inputs give the same hash.

Usage:
    python -m data.snapshot --asset-class us_equity --date 2026-11-13 --mode backtest --show AAPL,SPY
    python -m data.snapshot --asset-class crypto --date 2026-11-14 --mode live --store
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable, NamedTuple

from data import calendar as cal
from data import db
from data.ingest import (CRYPTO_SYMBOLS, calendar_closes, crypto_bar_closed, equity_bar_closed,
                         load_universe)

JOB = "build_snapshot"
VERSION = 1
MODES = ("live", "backtest")
ASSET_CLASSES = ("us_equity", "crypto")
HISTORY_DAYS = 252
ANNUAL = {"us_equity": 252, "crypto": 365}
RETURN_DAYS = (1, 5, 21, 63)


class SnapshotError(Exception):
    """The snapshot can't be built or stored as asked."""


class Bar(NamedTuple):
    """One split-adjusted daily bar, as agents see it."""
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


def canonical_json(obj: Any) -> str:
    """The one serialisation that is hashed: sorted keys, no spaces, no NaN."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=True)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Indicators (pure functions)
# ---------------------------------------------------------------------------

def _mean(xs: list[float]) -> float:
    return math.fsum(xs) / len(xs)


def _stdev(xs: list[float]) -> float:
    """Sample standard deviation (ddof=1)."""
    m = _mean(xs)
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def compute_fields(bars: list[Bar], asset_class: str) -> dict:
    """The §4 fields for one symbol from its bars (oldest first)."""
    c = [b.close for b in bars]
    v = [b.volume for b in bars]
    n = len(c)
    out: dict[str, Any] = {
        "n_bars": n,
        "last_date": bars[-1].date if bars else None,
        "last_close": c[-1] if bars else None,
        "dates_20": [b.date for b in bars[-20:]] if n >= 20 else None,
        "closes_20": c[-20:] if n >= 20 else None,
    }
    for k in RETURN_DAYS:
        out[f"ret_{k}"] = c[-1] / c[-1 - k] - 1 if n > k else None
    out["ma_20"] = _mean(c[-20:]) if n >= 20 else None
    out["ma_100"] = _mean(c[-100:]) if n >= 100 else None
    if n >= 21:
        rets = [c[i] / c[i - 1] - 1 for i in range(n - 20, n)]
        out["vol_20"] = _stdev(rets) * math.sqrt(ANNUAL[asset_class])
    else:
        out["vol_20"] = None
    sd = _stdev(c[-20:]) if n >= 20 else 0.0
    out["z_20"] = (c[-1] - out["ma_20"]) / sd if n >= 20 and sd > 0 else None
    vm = _mean(v[-20:]) if n >= 20 else 0.0
    out["volume_ratio_20"] = v[-1] / vm if n >= 20 and vm > 0 else None
    for key, val in out.items():
        if isinstance(val, float) and not math.isfinite(val):
            raise SnapshotError(f"non-finite {key}: {val}")
    return out


# ---------------------------------------------------------------------------
# Loading (the guard)
# ---------------------------------------------------------------------------

def cutoff_for(conn: sqlite3.Connection, asset_class: str, decision_date: date,
               trading: Any = None) -> datetime:
    """The protocol snapshot time for D: §3 close + 1:45 (equity), §13 D+1 00:10Z (crypto)."""
    if asset_class == "us_equity":
        return cal.equity_times(decision_date, conn, trading).snapshot_at
    if asset_class == "crypto":
        return cal.crypto_times(decision_date).snapshot_at
    raise SnapshotError(f"unknown asset class {asset_class!r}")


def _latest_versions(conn: sqlite3.Connection, symbols: list[str], decision_date: date) -> list[sqlite3.Row]:
    """Backtest read: latest version of every bar dated <= D, whatever its fetched_at."""
    names = [f":s{i}" for i in range(len(symbols))]
    params: dict[str, object] = {f"s{i}": s for i, s in enumerate(symbols)}
    params["d"] = decision_date.isoformat()
    sql = f"""
        SELECT p.* FROM prices AS p
        WHERE p.symbol IN ({', '.join(names)}) AND p.bar_date <= :d
          AND p.fetched_at = (SELECT MAX(q.fetched_at) FROM prices AS q
                              WHERE q.symbol = p.symbol AND q.bar_date = p.bar_date)
        ORDER BY p.symbol, p.bar_date"""
    return conn.execute(sql, params).fetchall()


def load_bars(conn: sqlite3.Connection, asset_class: str, symbols: list[str], decision_date: date,
              cutoff: datetime, mode: str,
              close_of: Callable[[date], datetime | None] | None = None) -> dict[str, list[Bar]]:
    """{symbol: [Bar, ...]} oldest first, at most HISTORY_DAYS, passing every guard.

    Each guard is applied on its own, so any one of them is enough to keep a
    future or unclosed bar out.
    """
    if mode not in MODES:
        raise SnapshotError(f"unknown mode {mode!r}")
    if not symbols:
        return {}
    if mode == "live":
        rows = db.prices_as_of(conn, db.utc_ts(cutoff), symbols)
    else:
        rows = _latest_versions(conn, symbols, decision_date)
    d = decision_date.isoformat()
    out: dict[str, list[Bar]] = {s: [] for s in symbols}
    for r in rows:
        if r["asset_class"] != asset_class or r["bar_date"] > d:
            continue
        ts = cal._parse_ts(r["bar_ts"])
        if ts > cutoff:
            continue
        if asset_class == "us_equity":
            if close_of is None:
                raise SnapshotError("equity bars need the calendar (close_of)")
            if not equity_bar_closed(ts, cutoff, close_of):
                continue
        elif not crypto_bar_closed(ts, cutoff):
            continue
        out[r["symbol"]].append(Bar(r["bar_date"], r["adj_open"], r["adj_high"], r["adj_low"],
                                    r["adj_close"], r["adj_volume"]))
    for s in out:
        out[s].sort(key=lambda b: b.date)
        out[s] = out[s][-HISTORY_DAYS:]
    return out


# ---------------------------------------------------------------------------
# The snapshot object
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Snapshot:
    asset_class: str
    decision_date: str
    mode: str
    cutoff_ts: str
    canonical: str               # canonical JSON of the payload (what is hashed and stored)
    sha256: str
    history_sha256: str
    _history: dict[str, tuple[Bar, ...]] = field(repr=False, compare=False)

    @property
    def payload(self) -> dict:
        """A fresh copy every time, so callers can't change the snapshot."""
        return json.loads(self.canonical)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._history))

    def history(self, symbol: str, n: int = HISTORY_DAYS) -> tuple[Bar, ...]:
        """The last n adjusted bars of symbol (oldest first), bounded by the snapshot's cutoff."""
        if not 1 <= n <= HISTORY_DAYS:
            raise SnapshotError(f"history is limited to 1..{HISTORY_DAYS} bars, asked for {n}")
        if symbol not in self._history:
            raise KeyError(symbol)
        return self._history[symbol][-n:]


def default_symbols(asset_class: str) -> list[str]:
    return load_universe() if asset_class == "us_equity" else list(CRYPTO_SYMBOLS)


def build_snapshot(conn: sqlite3.Connection, asset_class: str, decision_date: date, mode: str,
                   trading: Any = None, now: datetime | None = None,
                   symbols: Iterable[str] | None = None) -> Snapshot:
    """Build the §4 / §13 snapshot for decision_date. See the module docstring."""
    if asset_class not in ASSET_CLASSES:
        raise SnapshotError(f"unknown asset class {asset_class!r}")
    if mode not in MODES:
        raise SnapshotError(f"unknown mode {mode!r}")
    now = now or datetime.now(timezone.utc)
    symbols = sorted(symbols) if symbols is not None else sorted(default_symbols(asset_class))
    cutoff = cutoff_for(conn, asset_class, decision_date, trading)
    if mode == "backtest" and cutoff > now:
        raise SnapshotError(f"backtest snapshot for {decision_date}: cutoff {db.utc_ts(cutoff)} "
                            f"is in the future; use live mode")
    close_of = calendar_closes(conn, trading) if asset_class == "us_equity" else None
    bars = load_bars(conn, asset_class, symbols, decision_date, cutoff, mode, close_of)

    history = {s: [list(b) for b in bars[s]] for s in symbols}
    history_sha = sha256(canonical_json(history))
    payload = {
        "version": VERSION,
        "asset_class": asset_class,
        "decision_date": decision_date.isoformat(),
        "mode": mode,
        "cutoff_ts": db.utc_ts(cutoff),
        "history_sha256": history_sha,
        "symbols": {s: compute_fields(bars[s], asset_class) for s in symbols},
    }
    text = canonical_json(payload)
    return Snapshot(asset_class, decision_date.isoformat(), mode, db.utc_ts(cutoff), text, sha256(text),
                    history_sha, {s: tuple(bars[s]) for s in symbols})


# ---------------------------------------------------------------------------
# Storing (live only)
# ---------------------------------------------------------------------------

def _heartbeat(conn, run_id: str, event: str, detail: dict) -> None:
    with conn:
        conn.execute("INSERT INTO heartbeat (run_id, job, event, at, detail) VALUES (?, ?, ?, ?, ?)",
                     (run_id, JOB, event, db.utc_ts(), canonical_json(detail)))


def store_snapshot(conn: sqlite3.Connection, snap: Snapshot, trading: Any = None,
                   now: datetime | None = None) -> int:
    """Run the data checks as of the cutoff and store a live snapshot. Returns snapshot_id.

    A snapshot that fails the checks is still stored, with checks_passed = 0,
    so the decide step can refuse it.
    """
    from data import checks

    if snap.mode != "live":
        raise SnapshotError(f"only live snapshots are stored (this one is {snap.mode!r})")
    now = now or datetime.now(timezone.utc)
    cutoff = cal._parse_ts(snap.cutoff_ts)
    if now < cutoff:
        raise SnapshotError(f"can't store before the cutoff {snap.cutoff_ts}")
    run_id = uuid.uuid4().hex
    _heartbeat(conn, run_id, "started", {"asset_class": snap.asset_class, "date": snap.decision_date,
                                         "cutoff_ts": snap.cutoff_ts})
    try:
        firsts = [h[0].date for h in snap._history.values() if h]
        start = date.fromisoformat(min(firsts)) if firsts else date.fromisoformat(snap.decision_date)
        syms = list(snap.symbols)
        eq, cr = (syms, []) if snap.asset_class == "us_equity" else ([], syms)
        problems = checks.run_checks(conn, eq, cr, start, cutoff, trading)
        passed = not checks.has_errors(problems)
        with conn:
            sid = conn.execute(
                """INSERT INTO snapshots (asset_class, snapshot_date, cutoff_ts, built_at, payload,
                                          sha256, checks, checks_passed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (snap.asset_class, snap.decision_date, snap.cutoff_ts, db.utc_ts(now), snap.canonical,
                 snap.sha256, canonical_json(checks._jsonable(problems)), int(passed))).lastrowid
    except Exception as exc:  # noqa: BLE001 - every failure becomes an error heartbeat
        _heartbeat(conn, run_id, "error", {"error": f"{type(exc).__name__}: {exc}"[:500]})
        raise
    _heartbeat(conn, run_id, "finished", {"snapshot_id": sid, "sha256": snap.sha256,
                                          "checks_passed": passed, "problems": len(problems)})
    return sid


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _f(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def format_snapshot(snap: Snapshot, show: list[str]) -> str:
    p = snap.payload
    syms = p["symbols"]
    lines = [f"{p['asset_class']} {p['decision_date']} mode={p['mode']} cutoff={p['cutoff_ts']}",
             f"sha256 {snap.sha256}",
             "bars per symbol: " + ", ".join(f"{s}={syms[s]['n_bars']}" for s in sorted(syms))]
    cols = ("last_date", "last_close", "ret_1", "ret_5", "ret_21", "ret_63", "ma_20", "ma_100",
            "vol_20", "z_20", "volume_ratio_20")
    lines.append(f"{'symbol':<9} " + " ".join(f"{c:>15}" for c in cols))
    for s in show:
        if s in syms:
            lines.append(f"{s:<9} " + " ".join(f"{_f(syms[s][c]):>15}" for c in cols))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build (and optionally store) the daily snapshot.")
    p.add_argument("--asset-class", choices=ASSET_CLASSES, required=True)
    p.add_argument("--date", type=date.fromisoformat, required=True, metavar="YYYY-MM-DD")
    p.add_argument("--mode", choices=MODES, required=True)
    p.add_argument("--store", action="store_true", help="store it (live only)")
    p.add_argument("--show", default="", help="comma-separated symbols to print")
    p.add_argument("--db", default=str(db.DEFAULT_DB_PATH), help="database path")
    args = p.parse_args(argv)
    conn = db.init_db(args.db)
    try:
        snap = build_snapshot(conn, args.asset_class, args.date, args.mode)
        print(format_snapshot(snap, [s for s in args.show.split(",") if s]))
        if args.store:
            sid = store_snapshot(conn, snap)
            row = conn.execute("SELECT checks_passed FROM snapshots WHERE snapshot_id = ?", (sid,)).fetchone()
            print(f"stored snapshot {sid} (checks_passed={row[0]})")
    except (SnapshotError, cal.CalendarError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
