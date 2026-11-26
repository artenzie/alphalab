"""Corporate actions and the per-stock verdicts on them (protocol §7 rule 11, v1.18).

    from data import corporate_actions as ca
    ca.fetch(conn, client, symbols, now, sleep)        # every equity ingest: Alpaca -> corporate_actions
    ca.stale_symbols(conn, symbols, now)               # stocks whose stored history isn't on the current adjustment
    ca.review(conn, day, symbols, now)                 # runner, after ingest and before settle: symbol_events
    ca.split_verdicts(conn, day, now)                  # ledger: split verdicts up to day
    ca.frozen(conn, day, now)                          # ledger, runner, status: open freezes

    python -m data.corporate_actions show --experiment dev
    python -m data.corporate_actions resolve SYM EX_DATE --ratio 4 --experiment dev
    python -m data.corporate_actions resolve SYM EX_DATE --no-change --experiment dev

Fetch. Every type except cash dividends (§7 ignores them), for the 22 stocks and SPY, ex-dates
from today - 30 to today + 30 days. Each action is keyed type|symbol|ex_date and stored only when
its content differs from its latest stored version, with the fetch time: the same versioning as
`prices`. The symbol an action concerns is the one whose holders are affected: `symbol` for
splits, dividends, redemptions; the acquiree for mergers; the source for spin-offs and rights;
the old symbol for unit splits and name changes. An acquirer is unaffected and never frozen.
These rows never enter a snapshot.

Verdicts (symbol_events). For a stock and an ex-date, from what is stored by `now`:
  * the actions with that ex-date (latest version of each), and
  * the price step: raw ÷ adjusted close of the last stored bar before the ex-date (4 for a 4:1
    split once Alpaca has adjusted the history; 1 before it has, or with no split).
  split    exactly one forward or reverse split, ratio new_rate / old_rate > 0, and the step
           equals the ratio within SPLIT_TOL
  freeze   anything else that isn't "nothing": a step with no announcement, an announcement the
           step doesn't confirm (the lag case: Alpaca's announcement is known but the prices
           aren't adjusted yet), a missing or zero ratio, two actions at once, or any other type
           (a name change to a new ticker included)
  nothing  no action and no step
A name change that keeps the ticker (old_symbol == new_symbol: a new CUSIP) is set aside before
judging and logged only (review's `noted`, the runner's heartbeat), as XOM's of 2 Jul 2026.
The latest row per (symbol, ex_date) decides. review() writes a row only when the verdict
changes; a resolution references the freeze it ends (corrects_id). An open freeze is judged
again on every review, so it lifts by itself once a later fetch confirms the split (the ledger
applies it at that settle). A manual row (resolve) is never overridden by review.

Frozen stock (§7 rule 11): held by every agent at its last close before the ex-date (the raw
close of the last stored bar before it), its pending orders cancelled and new orders rejected.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from data import db

JOB = "corporate_actions"
TYPES = ("forward_split", "reverse_split", "unit_split", "stock_dividend", "spin_off", "cash_merger",
         "stock_merger", "stock_and_cash_merger", "redemption", "name_change", "worthless_removal",
         "rights_distribution")                     # every type except cash_dividend
SPLIT_TYPES = ("forward_split", "reverse_split")
WINDOW_DAYS = 30
SPLIT_TOL = 2e-3                                    # as data/checks.py RATIO_TOL
_SYMBOL_KEYS = ("symbol", "acquiree_symbol", "source_symbol", "old_symbol")
_DATE_KEYS = ("ex_date", "effective_date", "process_date")


class CorporateActionError(Exception):
    pass


# ---------------------------------------------------------------------------
# Fetch and store
# ---------------------------------------------------------------------------

def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _plain(item: Any) -> dict:
    if isinstance(item, dict):
        return dict(item)
    if hasattr(item, "model_dump"):
        return item.model_dump()
    return dict(vars(item))


def normalise(raw: Any, universe: Iterable[str]) -> list[dict]:
    """Alpaca's response ({type or plural type: [action, ...]}) -> rows for the universe's stocks."""
    data = getattr(raw, "data", raw) or {}
    universe = set(universe)
    out = []
    for key, items in data.items():
        t = str(key)
        t = t[:-1] if t.endswith("s") and t[:-1] in TYPES + ("cash_dividend",) else t
        if t == "cash_dividend":
            continue
        for item in items or []:
            it = _plain(item)
            sym = next((it[k] for k in _SYMBOL_KEYS if it.get(k)), None)
            if sym not in universe:
                continue
            d = next((it[k] for k in _DATE_KEYS if it.get(k)), None)
            if d is None:
                continue
            ex = str(d)[:10]
            out.append({"ca_key": f"{t}|{sym}|{ex}", "ca_type": t, "symbol": sym, "ex_date": ex,
                        "old_rate": _num(it.get("old_rate")), "new_rate": _num(it.get("new_rate")),
                        "raw": json.dumps(it, sort_keys=True, default=str)})
    return sorted(out, key=lambda r: r["ca_key"])


def store(conn: sqlite3.Connection, rows: list[dict], fetched_at: str) -> int:
    """Insert new versions only (content differs from the latest stored one), in one transaction."""
    n = 0
    with conn:
        for r in rows:
            last = conn.execute("SELECT raw FROM corporate_actions WHERE ca_key = ? ORDER BY fetched_at DESC, "
                                "ca_id DESC LIMIT 1", (r["ca_key"],)).fetchone()
            if last is not None and last[0] == r["raw"]:
                continue
            conn.execute("""INSERT INTO corporate_actions (ca_key, ca_type, symbol, ex_date, old_rate, new_rate,
                            raw, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                         (r["ca_key"], r["ca_type"], r["symbol"], r["ex_date"], r["old_rate"], r["new_rate"],
                          r["raw"], fetched_at))
            n += 1
    return n


def fetch(conn: sqlite3.Connection, client: Any, symbols: list[str], now: Callable[[], datetime],
          sleep: Callable[[float], None]) -> dict:
    """One fetch of the universe's corporate actions around today; returns counts for the heartbeat."""
    from alpaca.data.enums import CorporateActionsType
    from alpaca.data.requests import CorporateActionsRequest
    from data.ingest import with_retries

    today = now().date()
    req = CorporateActionsRequest(symbols=list(symbols), types=[CorporateActionsType(t) for t in TYPES],
                                  start=today - timedelta(days=WINDOW_DAYS), end=today + timedelta(days=WINDOW_DAYS))
    resp = with_retries(lambda: client.get_corporate_actions(req), "corporate actions", sleep)
    rows = normalise(resp, symbols)
    fetched_at = db.utc_ts(now())
    return {"actions": len(rows), "inserted": store(conn, rows, fetched_at), "fetched_at": fetched_at}


# ---------------------------------------------------------------------------
# Stored history vs the current adjustment
# ---------------------------------------------------------------------------

def stale_symbols(conn: sqlite3.Connection, symbols: Iterable[str], now: datetime) -> list[str]:
    """Stocks whose stored bars (latest versions) aren't on one consistent split adjustment.

    After a split, a 5-day ingest stores the re-adjusted recent bars while older bars keep the
    old adjustment: the adj/raw ratio then steps where the raw close doesn't jump, which the data
    checks report as split_inconsistent. Such a stock's whole history is re-fetched.
    """
    from data.checks import analyse_splits, load_bars

    out = []
    for sym, bars in load_bars(conn, symbols, date(1900, 1, 1), now).items():
        _, problems = analyse_splits(sym, "us_equity", bars, date(1900, 1, 1))
        if any(p["check"] == "split_inconsistent" and "latest bar" not in p["message"] for p in problems):
            out.append(sym)
    return out


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    event: str                 # 'split' | 'freeze' | 'none'
    ratio: float | None
    reason: str
    action_ids: tuple[int, ...] = ()
    noted: tuple[dict, ...] = ()     # actions logged only (a name change that keeps the ticker)


def actions_for(conn: sqlite3.Connection, symbol: str, ex_date: str, now: datetime) -> list[sqlite3.Row]:
    """Latest version, fetched by now, of each action for symbol with that ex-date."""
    return conn.execute(
        """SELECT c.* FROM corporate_actions AS c WHERE c.symbol = ? AND c.ex_date = ? AND c.fetched_at <= ?
             AND c.ca_id = (SELECT q.ca_id FROM corporate_actions AS q WHERE q.ca_key = c.ca_key
                            AND q.fetched_at <= ? ORDER BY q.fetched_at DESC, q.ca_id DESC LIMIT 1)
           ORDER BY c.ca_key""", (symbol, ex_date, db.utc_ts(now), db.utc_ts(now))).fetchall()


def bar_before(conn: sqlite3.Connection, symbol: str, ex_date: str, now: datetime) -> sqlite3.Row | None:
    """Latest version, fetched by now, of the last stored bar dated before ex_date."""
    ts = db.utc_ts(now)
    return conn.execute(
        """SELECT p.* FROM prices AS p WHERE p.symbol = ? AND p.bar_date < ? AND p.fetched_at <= ? AND p.bar_ts <= ?
           ORDER BY p.bar_date DESC, p.fetched_at DESC, p.price_id DESC LIMIT 1""",
        (symbol, ex_date, ts, ts)).fetchone()


def price_step(conn: sqlite3.Connection, symbol: str, ex_date: str, now: datetime) -> float | None:
    """raw ÷ adjusted close of the last bar before ex_date: the split factor the prices show."""
    b = bar_before(conn, symbol, ex_date, now)
    if b is None or not b["adj_close"]:
        return None
    return b["close"] / b["adj_close"]


def _same(a: float, b: float) -> bool:
    return abs(a / b - 1) <= SPLIT_TOL


def same_ticker_rename(action: Mapping[str, Any]) -> bool:
    """A name change that keeps the ticker (a new CUSIP, say): logged only, never frozen (v1.18)."""
    if action["ca_type"] != "name_change":
        return False
    raw = json.loads(action["raw"] or "{}")
    return bool(raw.get("new_symbol")) and raw.get("new_symbol") == raw.get("old_symbol") == action["symbol"]


def judge(conn: sqlite3.Connection, symbol: str, ex_date: str, now: datetime) -> Verdict:
    every = actions_for(conn, symbol, ex_date, now)
    noted = tuple({"ca_id": a["ca_id"], "symbol": symbol, "ex_date": ex_date, "ca_type": a["ca_type"],
                   "reason": "name change keeps the ticker: logged only"} for a in every if same_ticker_rename(a))
    acts = [a for a in every if not same_ticker_rename(a)]
    v = _verdict(acts, symbol, ex_date, price_step(conn, symbol, ex_date, now))
    return Verdict(v.event, v.ratio, v.reason, v.action_ids, noted)


def _verdict(acts: list, symbol: str, ex_date: str, step: float | None) -> Verdict:
    ids = tuple(a["ca_id"] for a in acts)
    stepped = step is not None and not _same(step, 1.0)
    if not acts:
        if stepped:
            return Verdict("freeze", None, f"prices show a {step:.6g}x step before {ex_date} with no announcement")
        return Verdict("none", None, "no action")
    if len(acts) > 1:
        return Verdict("freeze", None, f"{len(acts)} actions on {ex_date}: "
                       + ", ".join(a["ca_type"] for a in acts), ids)
    a = acts[0]
    if a["ca_type"] not in SPLIT_TYPES:
        return Verdict("freeze", None, f"{a['ca_type']} on {ex_date}", ids)
    old, new = a["old_rate"], a["new_rate"]
    if not old or not new or old <= 0 or new <= 0:
        return Verdict("freeze", None, f"{a['ca_type']} on {ex_date} without a usable ratio "
                       f"(old_rate {old}, new_rate {new})", ids)
    ratio = new / old
    if step is None:
        return Verdict("freeze", ratio, f"{a['ca_type']} {new:g}:{old:g} on {ex_date}: no stored bar before it", ids)
    if not _same(step, ratio):
        return Verdict("freeze", ratio, f"{a['ca_type']} {new:g}:{old:g} on {ex_date} not confirmed by the prices "
                       f"(step {step:.6g}x)", ids)
    return Verdict("split", ratio, f"{a['ca_type']} {new:g}:{old:g} on {ex_date}, confirmed by the prices", ids)


def latest_event(conn: sqlite3.Connection, symbol: str, ex_date: str, now: datetime | None = None) -> sqlite3.Row | None:
    sql = "SELECT * FROM symbol_events WHERE symbol = ? AND ex_date = ?"
    params: list[Any] = [symbol, ex_date]
    if now is not None:
        sql += " AND decided_at <= ?"
        params.append(db.utc_ts(now))
    return conn.execute(sql + " ORDER BY symbol_event_id DESC LIMIT 1", params).fetchone()


def _latest_events(conn: sqlite3.Connection, now: datetime) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT e.* FROM symbol_events AS e WHERE e.decided_at <= :now
             AND e.symbol_event_id = (SELECT MAX(q.symbol_event_id) FROM symbol_events AS q
                                      WHERE q.symbol = e.symbol AND q.ex_date = e.ex_date AND q.decided_at <= :now)
           ORDER BY e.symbol, e.ex_date""", {"now": db.utc_ts(now)}).fetchall()


def _write(conn, symbol: str, ex_date: str, v: Verdict, now: datetime, source: str = "auto",
           corrects: int | None = None) -> int:
    return conn.execute(
        """INSERT INTO symbol_events (symbol, ex_date, event, ratio, reason, corporate_action_ids, source,
           decided_at, corrects_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (symbol, ex_date, v.event, v.ratio, v.reason, json.dumps(list(v.action_ids)), source, db.utc_ts(now),
         corrects)).lastrowid


def review(conn: sqlite3.Connection, day: date, symbols: Iterable[str], now: datetime,
           noted: list[dict] | None = None) -> list[dict]:
    """Judge every stock for ex-date `day` (and any action dated since its last bar), and every
    open freeze again. Writes a symbol_events row where the verdict changed; returns those rows.
    Actions logged only (a name change that keeps the ticker) are appended to `noted`."""
    written = []
    d = day.isoformat()
    with conn:
        todo: set[tuple[str, str]] = set()
        for sym in symbols:
            todo.add((sym, d))
            last = bar_before(conn, sym, d, now)
            lo = last["bar_date"] if last is not None else "0000-00-00"
            for r in conn.execute("SELECT DISTINCT ex_date FROM corporate_actions WHERE symbol = ? AND ex_date > ? "
                                  "AND ex_date <= ? AND fetched_at <= ?", (sym, lo, d, db.utc_ts(now))):
                todo.add((sym, r[0]))
        for e in _latest_events(conn, now):
            if e["event"] == "freeze" and e["ex_date"] <= d:
                todo.add((e["symbol"], e["ex_date"]))
        for sym, ex in sorted(todo):
            prev = latest_event(conn, sym, ex, now)
            if prev is not None and prev["source"] == "manual":
                continue
            v = judge(conn, sym, ex, now)
            if noted is not None:
                noted.extend(v.noted)
            if prev is None:
                if v.event == "none":
                    continue
            elif v.event == "none":
                if prev["event"] != "freeze":
                    continue
                v = Verdict("unfreeze", None, "no action or price step any more", v.action_ids)
            elif prev["event"] == v.event and prev["ratio"] == v.ratio and prev["reason"] == v.reason:
                continue
            elif prev["event"] == "split":
                continue        # a split once decided stays: the ledger may already have applied it
            corrects = prev["symbol_event_id"] if prev is not None else None
            sid = _write(conn, sym, ex, v, now, corrects=corrects)
            written.append({"symbol_event_id": sid, "symbol": sym, "ex_date": ex, "event": v.event,
                            "ratio": v.ratio, "reason": v.reason})
    return written


def frozen(conn: sqlite3.Connection, day: date, now: datetime) -> dict[str, sqlite3.Row]:
    """Open freezes with ex_date <= day, as known at now: symbol -> its latest (freeze) row."""
    out = {}
    for e in _latest_events(conn, now):
        if e["event"] == "freeze" and e["ex_date"] <= day.isoformat():
            out.setdefault(e["symbol"], e)
    return out


def open_freezes(conn: sqlite3.Connection, now: datetime) -> list[dict]:
    """Every open freeze (any ex-date), for the status page, the summary and the alerts."""
    return [dict(e) for e in _latest_events(conn, now) if e["event"] == "freeze"]


def frozen_mark(conn: sqlite3.Connection, symbol: str, ex_date: str, now: datetime) -> float:
    """Its last close before the event: the raw close of the last stored bar before ex_date."""
    b = bar_before(conn, symbol, ex_date, now)
    if b is None:
        raise CorporateActionError(f"{symbol}: no stored bar before {ex_date} to hold it at")
    return b["close"]


def frozen_marks(conn: sqlite3.Connection, day: date, now: datetime) -> dict[str, float]:
    return {s: frozen_mark(conn, s, e["ex_date"], now) for s, e in frozen(conn, day, now).items()}


def split_verdicts(conn: sqlite3.Connection, day: date, now: datetime) -> list[sqlite3.Row]:
    """Every (symbol, ex_date) whose latest verdict, known by now, is 'split', ex-date on or before day.
    The ledger decides per agent which it hasn't passed yet (execution/ledger_db.py)."""
    return [e for e in _latest_events(conn, now) if e["event"] == "split" and e["ex_date"] <= day.isoformat()]


def was_frozen(conn: sqlite3.Connection, symbol: str, ex_date: str) -> bool:
    return conn.execute("SELECT 1 FROM symbol_events WHERE symbol = ? AND ex_date = ? AND event = 'freeze' LIMIT 1",
                        (symbol, ex_date)).fetchone() is not None


def resolve(conn: sqlite3.Connection, symbol: str, ex_date: str, now: datetime, ratio: float | None,
            note: str = "") -> int:
    """A manual resolution of an open freeze: apply ratio, or lift with no change (ratio None)."""
    prev = latest_event(conn, symbol, ex_date)
    if prev is None or prev["event"] != "freeze":
        raise CorporateActionError(f"{symbol} {ex_date} is not frozen")
    if ratio is not None and ratio <= 0:
        raise CorporateActionError("ratio must be > 0")
    v = (Verdict("split", ratio, f"manual: split ratio {ratio:g}" + (f" ({note})" if note else ""))
         if ratio is not None else Verdict("unfreeze", None, "manual: no change" + (f" ({note})" if note else "")))
    with conn:
        return _write(conn, symbol, ex_date, v, now, source="manual", corrects=prev["symbol_event_id"])


def main(argv: list[str] | None = None) -> int:
    from runner import experiment as ex

    p = argparse.ArgumentParser(description="Corporate actions and freezes (§7 rule 11).")
    p.add_argument("--experiment", required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="open freezes and the latest verdicts")
    r = sub.add_parser("resolve", help="resolve a freeze by hand (a new entry referencing it)")
    r.add_argument("symbol")
    r.add_argument("ex_date", type=date.fromisoformat)
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--ratio", type=float, help="shares multiplier: 4 for a 4:1 split, 0.1 for 1:10")
    g.add_argument("--no-change", action="store_true", help="lift the freeze with nothing to apply")
    r.add_argument("--note", default="")
    a = p.parse_args(argv)
    conn = db.connect(ex.path_for(a.experiment))
    now = datetime.now(timezone.utc)
    try:
        if a.cmd == "show":
            for e in _latest_events(conn, now):
                print(f"{e['symbol']:<6} {e['ex_date']}  {e['event']:<8} ratio {e['ratio']}  {e['source']}: {e['reason']}")
            return 0
        sid = resolve(conn, a.symbol, a.ex_date.isoformat(), now, None if a.no_change else a.ratio, a.note)
        print(f"symbol_events row {sid} written")
        return 0
    except CorporateActionError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
