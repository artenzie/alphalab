"""Data checks on the stored daily bars. Run after every ingest (protocol §3, 17:30 ET).

    from data import checks
    problems = checks.run_checks(conn, equity, crypto, start=date(2024, 11, 1), now=now)
    # [] means all good; otherwise a list of plain dicts (JSON-serialisable,
    # no NaN), ready for snapshots.checks:
    # {"check": "missing_day", "severity": "error", "symbol": "AAPL",
    #  "date": "2025-03-04", "message": "...", "detail": {...}}

Every check reads the latest stored version of each bar as of `now`
(db.prices_as_of), the same point-in-time read the snapshot uses. The window
is [start, now]; one bar before `start` is read as well, so the first day's
move and split ratio have something to compare with.

Checks (severity):
  missing_day       error    a trading day (equities, from the cached calendar)
                             or calendar day (crypto) in the window has no bar
  unexpected_day    error    a bar on a day that isn't expected (e.g. a holiday)
  bad_bar           error    low <= open, close <= high; prices > 0; volume >= 0
                             (raw and split-adjusted)
  suspicious_move   warning  adjusted close moves > 25% (equity) / 40% (crypto)
                             from the previous stored bar: for a human to look at
  split_inconsistent error   adj/raw ratios not explained by clean splits (below)
  stale             error    latest bar isn't the latest closed session / day
  unclosed_bar      error    a bar for a session that hasn't closed (rule 1)
  calendar_unavailable error the calendar can't say which days were sessions

Split consistency. For each bar r = adj_close / close. On one day the open,
high, low and close ratios must agree, and adj_volume * adj_close must equal
volume * close. Over time r is piecewise constant: each change is a split
step with factor f = r_after / r_before (10 for a 10:1 split), which must look like a real split
(p:q with the smaller term <= 4 and the larger <= 50: 2:1, 3:2, 10:1, 1:10 ...;
a looser rule would match almost any factor near 1), and the raw close must jump with it, so that the adjusted close
stays within the suspicious-move threshold across the step. Each bar is
compared with the ratio at the start of its segment, so a slow drift is caught
too, not just a jump. The latest bar
must have r = 1 (Alpaca adjusts relative to today). Clean steps are not
problems; `find_splits` lists them.

Alert (`check_and_alert`): heartbeat rows with job 'data_checks' and the
ingest run's run_id: 'started', then
  * any error      -> event 'error',    detail.status 'error'    -> exit 1
  * warnings only  -> event 'finished', detail.status 'warning'  -> exit 0
  * nothing        -> event 'finished', detail.status 'ok'       -> exit 0
(A +30% earnings day must never stop the daily run. The phone alert comes in
Phase 4 and reads detail.status.)

Usage:
    python -m data.checks --start 2024-11-01     # check the stored history
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from typing import Any, Iterable

from data import calendar as cal
from data import db
from data.ingest import CRYPTO_SYMBOLS, date_ranges, load_universe

JOB = "data_checks"
MOVE_LIMIT = {"us_equity": 0.25, "crypto": 0.40}
RATIO_TOL = 2e-3          # relative; allows for Alpaca rounding adjusted prices
MAX_SPLIT_SMALL = 4       # split p:q: the smaller term at most 4 ...
MAX_SPLIT_LARGE = 50      # ... and the larger at most 50


def problem(check: str, severity: str, symbol: str | None, day: str | None,
            message: str, **detail: Any) -> dict:
    return {"check": check, "severity": severity, "symbol": symbol, "date": day,
            "message": message, "detail": _jsonable(detail)}


def _jsonable(v: Any) -> Any:
    """Plain JSON types only; NaN/inf become strings (SQLite's json_valid rejects them)."""
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, float) and not math.isfinite(v):
        return str(v)
    return v


def _finite_pos(*xs: float) -> bool:
    return all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0 for x in xs)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_bars(conn: sqlite3.Connection, symbols: Iterable[str], start: date,
              now: datetime) -> dict[str, list[dict]]:
    """{symbol: [bar, ...]} by date: the window plus the last bar before it.

    Latest version of each bar fetched at or before `now`.
    """
    symbols = list(symbols)
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    if not symbols:
        return out
    for r in db.prices_as_of(conn, db.utc_ts(now), symbols):
        out[r["symbol"]].append(dict(r))
    s0 = start.isoformat()
    for s, bars in out.items():
        before = [b for b in bars if b["bar_date"] < s0]
        out[s] = before[-1:] + [b for b in bars if b["bar_date"] >= s0]
    return out


def expected_equity_days(conn: sqlite3.Connection, start: date, now: datetime,
                         trading: Any = None) -> list[str]:
    """Sessions from `start` whose (real, possibly early) close is at or before `now`."""
    out = []
    d, last = start, now.astimezone(cal.NY).date()
    while d <= last:
        s = cal.session(d, conn, trading)
        if s is not None and s.close <= now:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def expected_crypto_days(start: date, now: datetime) -> list[str]:
    """Every calendar day from `start` whose 00:00 UTC bar has closed at `now`."""
    last = now.astimezone(timezone.utc).date() - timedelta(days=1)
    return [(start + timedelta(days=i)).isoformat() for i in range(max((last - start).days + 1, 0))]


# ---------------------------------------------------------------------------
# Checks (each takes one symbol's bars, sorted by date)
# ---------------------------------------------------------------------------

def check_days(symbol: str, bars: list[dict], expected: list[str], start: date) -> list[dict]:
    """missing_day / unexpected_day, grouped into one problem per symbol each."""
    have = {b["bar_date"] for b in bars if b["bar_date"] >= start.isoformat()}
    exp = set(expected)
    out = []
    missing = sorted(exp - have)
    if missing:
        ranges = date_ranges(missing, expected)
        out.append(problem("missing_day", "error", symbol, missing[0],
                           f"{symbol}: {len(missing)} missing day(s): {', '.join(ranges)}",
                           dates=missing, ranges=ranges))
    extra = sorted(have - exp)
    if extra:
        out.append(problem("unexpected_day", "error", symbol, extra[0],
                           f"{symbol}: bar(s) on day(s) that aren't expected: {', '.join(extra)}",
                           dates=extra))
    return out


def bar_faults(b: dict) -> list[str]:
    """Why a bar is bad (empty = fine), for raw and adjusted values."""
    faults = []
    for pre, name in (("", "raw"), ("adj_", "adjusted")):
        o, h, l, c = (b[pre + k] for k in ("open", "high", "low", "close"))
        v = b[pre + "volume"]
        bad = [k for k, x in zip(("open", "high", "low", "close"), (o, h, l, c)) if not _finite_pos(x)]
        if bad:
            faults.append(f"{name} {'/'.join(bad)} not a positive number")
        else:
            if not l <= o <= h:
                faults.append(f"{name} open {o} outside low..high {l}..{h}")
            if not l <= c <= h:
                faults.append(f"{name} close {c} outside low..high {l}..{h}")
        if not (isinstance(v, (int, float)) and math.isfinite(v) and v >= 0):
            faults.append(f"{name} volume {v} is negative or not a number")
    return faults


def check_bad_bars(symbol: str, bars: list[dict], start: date) -> list[dict]:
    out = []
    for b in bars:
        if b["bar_date"] < start.isoformat():
            continue
        faults = bar_faults(b)
        if faults:
            out.append(problem("bad_bar", "error", symbol, b["bar_date"],
                               f"{symbol} {b['bar_date']}: " + "; ".join(faults), faults=faults,
                               bar={k: b[k] for k in ("open", "high", "low", "close", "volume", "adj_open",
                                                      "adj_high", "adj_low", "adj_close", "adj_volume")}))
    return out


def check_moves(symbol: str, asset_class: str, bars: list[dict], start: date) -> list[dict]:
    limit = MOVE_LIMIT[asset_class]
    out = []
    for prev, cur in zip(bars, bars[1:]):
        if cur["bar_date"] < start.isoformat() or not _finite_pos(prev["adj_close"], cur["adj_close"]):
            continue
        change = cur["adj_close"] / prev["adj_close"] - 1
        if abs(change) > limit:
            out.append(problem("suspicious_move", "warning", symbol, cur["bar_date"],
                               f"{symbol} {cur['bar_date']}: adjusted close {change:+.1%} since "
                               f"{prev['bar_date']} (limit ±{limit:.0%}); check by hand",
                               change=round(change, 6), prev_date=prev["bar_date"],
                               prev_adj_close=prev["adj_close"], adj_close=cur["adj_close"]))
    return out


def _close(a: float, b: float, tol: float = RATIO_TOL) -> bool:
    return abs(a - b) <= tol * max(abs(a), abs(b))


def split_factor(f: float) -> Fraction | None:
    """f as a split-like fraction p/q (smaller term <= 4, larger <= 50, not 1), or None."""
    if not _finite_pos(f):
        return None
    frac = Fraction(f).limit_denominator(MAX_SPLIT_SMALL) if f >= 1 else \
        1 / Fraction(1 / f).limit_denominator(MAX_SPLIT_SMALL)
    small, large = sorted((frac.numerator, frac.denominator))
    if frac != 1 and small <= MAX_SPLIT_SMALL and large <= MAX_SPLIT_LARGE and _close(float(frac), f):
        return frac
    return None


def analyse_splits(symbol: str, asset_class: str, bars: list[dict],
                   start: date) -> tuple[list[dict], list[dict]]:
    """(splits, problems) from the adj/raw ratios of one symbol's bars."""
    splits: list[dict] = []
    problems: list[dict] = []
    s0 = start.isoformat()
    ok = [b for b in bars if not bar_faults(b)]  # bad bars are reported by check_bad_bars

    def flag(day: str, msg: str, **detail: Any) -> None:
        problems.append(problem("split_inconsistent", "error", symbol, day, f"{symbol} {day}: {msg}", **detail))

    for b in ok:
        if b["bar_date"] < s0:
            continue
        ratios = {k: b["adj_" + k] / b[k] for k in ("open", "high", "low", "close")}
        if not _close(max(ratios.values()), min(ratios.values())):
            flag(b["bar_date"], "open/high/low/close adjustment ratios differ",
                 ratios={k: round(v, 6) for k, v in ratios.items()})
        if b["volume"] > 0 and not _close(b["adj_volume"] * b["adj_close"], b["volume"] * b["close"]):
            flag(b["bar_date"], "adjusted volume doesn't match the price adjustment",
                 volume=b["volume"], adj_volume=b["adj_volume"], ratio=round(ratios["close"], 6))

    limit = MOVE_LIMIT[asset_class]
    ref = ok[0]["adj_close"] / ok[0]["close"] if ok else None  # ratio at the start of the segment
    for prev, cur in zip(ok, ok[1:]):
        r1 = cur["adj_close"] / cur["close"]
        if _close(ref, r1):
            continue
        r0, ref = ref, r1  # a new segment starts at cur
        if cur["bar_date"] < s0:
            continue
        f = r1 / r0  # shares multiplier: 10.0 for a 10:1 split (pre-split bars carry r = 0.1)
        frac = split_factor(f)
        adj_move = cur["adj_close"] / prev["adj_close"] - 1
        raw_move = cur["close"] / prev["close"] - 1
        detail = dict(prev_date=prev["bar_date"], ratio_before=round(r0, 6), ratio_after=round(r1, 6),
                      factor=round(f, 6), raw_move=round(raw_move, 6), adj_move=round(adj_move, 6))
        if frac is None:
            flag(cur["bar_date"], f"adjustment ratio changes by {f:.4f}, not a simple split factor", **detail)
        elif abs(adj_move) > limit:
            flag(cur["bar_date"], f"adjustment ratio changes by {frac} but the raw close moves "
                 f"{raw_move:+.1%}, so the adjusted close jumps {adj_move:+.1%}: adjustment on the wrong day "
                 f"or bars adjusted at different times", **detail)
        else:
            splits.append({"symbol": symbol, "date": cur["bar_date"], "factor": f"{frac.numerator}:{frac.denominator}",
                           "ratio_before": round(r0, 6), "ratio_after": round(r1, 6)})

    if ok and ok[-1]["bar_date"] >= s0:
        last = ok[-1]
        r = last["adj_close"] / last["close"]
        if not _close(r, 1.0):
            flag(last["bar_date"], f"latest bar has adjustment ratio {r:.6f}, should be 1",
                 ratio=round(r, 6))
    return splits, problems


def check_staleness(symbol: str, bars: list[dict], expected_last: str | None) -> list[dict]:
    if expected_last is None:
        return []
    last = bars[-1]["bar_date"] if bars else None
    if last is None or last < expected_last:
        return [problem("stale", "error", symbol, last,
                        f"{symbol}: latest bar is {last or 'none'}, expected {expected_last}",
                        latest=last, expected=expected_last)]
    if last > expected_last:
        return [problem("unclosed_bar", "error", symbol, last,
                        f"{symbol}: bar for {last}, which hasn't closed (latest closed: {expected_last})",
                        latest=last, expected=expected_last)]
    return []


# ---------------------------------------------------------------------------
# Running them
# ---------------------------------------------------------------------------

def _analyse(conn, equity_symbols, crypto_symbols, start: date, now: datetime,
             trading: Any = None) -> tuple[list[dict], list[dict]]:
    if now.tzinfo is None:
        raise ValueError("naive now")
    equity_symbols, crypto_symbols = list(equity_symbols), list(crypto_symbols)
    problems: list[dict] = []
    splits: list[dict] = []
    eq_days: list[str] | None = None
    if equity_symbols:
        try:
            eq_days = expected_equity_days(conn, start, now, trading)
        except cal.CalendarError as exc:
            problems.append(problem("calendar_unavailable", "error", None, None,
                                    f"market calendar unavailable, equity days not checked: {exc}"))
    cr_days = expected_crypto_days(start, now)
    bars = load_bars(conn, equity_symbols + crypto_symbols, start, now)
    for sym in equity_symbols + crypto_symbols:
        asset_class = "us_equity" if sym in equity_symbols else "crypto"
        b = bars[sym]
        expected = eq_days if asset_class == "us_equity" else cr_days
        if expected is not None:
            problems += check_days(sym, b, expected, start)
            problems += check_staleness(sym, b, expected[-1] if expected else None)
        problems += check_bad_bars(sym, b, start)
        problems += check_moves(sym, asset_class, b, start)
        s, p = analyse_splits(sym, asset_class, b, start)
        splits += s
        problems += p
    return problems, splits


def run_checks(conn: sqlite3.Connection, equity_symbols: Iterable[str], crypto_symbols: Iterable[str],
               start: date, now: datetime, trading: Any = None) -> list[dict]:
    """All checks on [start, now]. Returns the problems; [] means all good."""
    return _analyse(conn, equity_symbols, crypto_symbols, start, now, trading)[0]


def find_splits(conn: sqlite3.Connection, equity_symbols: Iterable[str], start: date,
                now: datetime) -> list[dict]:
    """Clean split steps in [start, now] (not problems), e.g. {"symbol", "date", "factor": "10:1"}."""
    out = []
    for sym, bars in load_bars(conn, equity_symbols, start, now).items():
        out += analyse_splits(sym, "us_equity", bars, start)[0]
    return out


def has_errors(problems: list[dict]) -> bool:
    return any(p["severity"] == "error" for p in problems)


def _heartbeat(conn, run_id: str, event: str, detail: dict) -> None:
    with conn:
        conn.execute("INSERT INTO heartbeat (run_id, job, event, at, detail) VALUES (?, ?, ?, ?, ?)",
                     (run_id, JOB, event, db.utc_ts(), json.dumps(detail, sort_keys=True, allow_nan=False)))


def check_and_alert(conn: sqlite3.Connection, run_id: str, equity_symbols, crypto_symbols,
                    start: date, now: datetime, trading: Any = None) -> tuple[int, list[dict], list[dict]]:
    """Run the checks and write the heartbeat alert. Returns (exit code, problems, splits)."""
    _heartbeat(conn, run_id, "started", {"start": start.isoformat(), "now": db.utc_ts(now)})
    problems, splits = _analyse(conn, equity_symbols, crypto_symbols, start, now, trading)
    counts = {sev: sum(p["severity"] == sev for p in problems) for sev in ("error", "warning")}
    status = "error" if counts["error"] else "warning" if counts["warning"] else "ok"
    detail = {"status": status, "counts": counts, "problems": problems, "splits": splits,
              "start": start.isoformat(), "now": db.utc_ts(now)}
    _heartbeat(conn, run_id, "error" if status == "error" else "finished", detail)
    return (1 if status == "error" else 0), problems, splits


def format_report(problems: list[dict], splits: list[dict]) -> str:
    lines = []
    for sev in ("error", "warning"):
        ps = [p for p in problems if p["severity"] == sev]
        lines.append(f"{len(ps)} {sev}(s)")
        lines += [f"  [{p['check']}] {p['message']}" for p in ps]
    lines.append(f"{len(splits)} split(s) in the window")
    lines += [f"  {s['symbol']} {s['date']} {s['factor']}" for s in splits]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run the data checks on the stored daily bars.")
    p.add_argument("--start", type=date.fromisoformat, required=True, metavar="YYYY-MM-DD")
    p.add_argument("--db", default=str(db.DEFAULT_DB_PATH), help="database path")
    args = p.parse_args(argv)
    conn = db.init_db(args.db)
    try:
        run_id = uuid.uuid4().hex
        code, problems, splits = check_and_alert(conn, run_id, load_universe(), CRYPTO_SYMBOLS,
                                                 args.start, datetime.now(timezone.utc))
        print(f"data checks run {run_id}")
        print(format_report(problems, splits))
        return code
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
