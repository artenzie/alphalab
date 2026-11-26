"""The ledger's database layer (protocol §7 v1.8, §9): load and store what execution/ledger.py computes.

Two calls per agent per trading day, in this order:

    res = settle_day(conn, agent_id, day, now)          # after day's bars are stored:
                                                        #   fill pending orders at day's open,
                                                        #   accrue, mark at day's close
    placed = place_orders(conn, agent_id, day, closes,  # after the decision on day:
                          targets, created_at,          #   margin check, then orders
                          decision_id)                  #   (pending until settle_day(D+1))

All writes are inserts into the append-only tables (orders, fills, positions,
equity, risk_events); each call writes everything or nothing, in one
transaction. Both are restart-safe: settle_day returns 'already_settled' if the
agent already has fills or an equity row on that day (ledger rule 10), and
place_orders returns 'already_placed' if the agent has unfilled orders.

No early prices (rule 11): a day is settled only from bars dated `day` whose
session has closed by `now` and that were fetched at or before `now`. If any
symbol needed (held, or with a pending order) lacks such a bar, PriceNotAvailable
is raised and nothing is written. Delisted stocks must be passed in `delisted`
(symbol -> last traded price): they are closed at that price first, and their
pending orders are cancelled.

Modes:
  live      raw open/close (real shares, as in the Alpaca mirrors). If the
            previous session's bar of a symbol held or ordered now has
            adj/raw != 1, a split happened in between: SplitDetected stops the
            ledger for a manual correction (rule 11) rather than sizing across it.
  backtest  split-adjusted open/close, matching backtest snapshots; no split guard.

Crypto (C0, C1; §7 rule 4, v1.13). Every calendar day is a settle day: day
D+1 fills decision D's orders and marks at D+1's daily close (00:00Z on D+2).
Pending orders are those created by D+1's snapshot cutoff (00:10Z on D+2), the
latest a decision D may be placed (place_orders returns 'missed' after it).
They fill at the open of the first 1-minute bar (crypto_fill_bars) in
[t_ref, t_ref + 60 min), t_ref = the later of 00:15Z on D+1 and the orders'
created_at (the time the day's last crypto decision was recorded, shared by
every crypto agent: runner/daily.py), rounded up to the whole minute, so a fill
is never earlier than its order; plus 5 bps and the 0.25% fee
(ledger.CRYPTO_FEE), the cash floor included. The bar must have closed by
`now` and been fetched by `now` (rule 11); filled_at is its bar_ts and the
fill detail names its fill_bar_id. A coin with no bar in the window is
cancelled ('no_fill_bar', filled_at = t_ref) once crypto_fill_windows shows the
whole window was fetched after it closed; until then PriceNotAvailable. No split
guard or delisting for crypto.

Cash floor (rule 12): for agents with ledger.has_cash_floor, the pending
orders go through ledger.cash_floor before filling: sells first, buys scaled
pro rata so cash can't end below 0. Each scaled buy gets a 'cash_floor'
risk_events row (with its order's decision_id); its fill row has the scaled
qty and the event id in detail, or is 'cancelled' if scaled to under $1. The
orders rows keep the quantities as placed (append-only).

Accounts are rebuilt from the last equity row and its positions rows; with no
rows the agent starts with START_CASH. Corrections (corrects_id) aren't
written by this module; when reading, the newest row per day and symbol wins.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping

from data import calendar as cal
from data import corporate_actions as ca
from data import db
from data.fill_prices import MINUTE, WINDOW
from data.ingest import crypto_bar_closed, equity_bar_closed
from execution import ledger as L
from execution.ledger import Account, LedgerError, Order, Position

MODES = ("live", "backtest")
SPLIT_TOL = 2e-3          # as data/checks.py RATIO_TOL: Alpaca rounds adjusted prices


class PriceNotAvailable(LedgerError):
    """A price the fill or mark needs isn't stored and closed yet (ledger rule 11)."""


class SplitDetected(LedgerError):
    """A price step with no verdict in symbol_events: the corporate-actions review hasn't run (v1.18)."""


@dataclass(frozen=True)
class DayResult:
    status: str                       # 'settled' | 'already_settled'
    fills: tuple[L.Fill, ...] = ()
    cancelled: tuple[str, ...] = ()   # symbols whose pending orders were cancelled (delisted; crypto no_fill_bar)
    mark: L.Mark | None = None
    borrow: float = 0.0
    interest: float = 0.0
    floor_events: tuple[dict, ...] = ()   # rule 12 clips
    splits: tuple[dict, ...] = ()         # rule 11 (v1.18): splits applied before the fills
    frozen: tuple[str, ...] = ()          # rule 11 (v1.18): frozen stocks at this settle


@dataclass(frozen=True)
class Placed:
    status: str                       # 'placed' | 'already_placed' | 'missed' (crypto, too late: nothing written)
    orders: tuple[Order, ...] = ()
    order_ids: tuple[int, ...] = ()
    margin_events: tuple[dict, ...] = ()
    margin_superseded: bool = False


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _asset_class(conn: sqlite3.Connection, agent_id: str) -> str:
    row = conn.execute("SELECT asset_class FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    if row is None:
        raise LedgerError(f"unknown agent {agent_id!r}")
    return row[0]


def load_account(conn: sqlite3.Connection, agent_id: str, *, before: date | None = None,
                 as_of: date | None = None) -> Account:
    """The book at the last close strictly before `before` (or at or before `as_of`)."""
    if (before is None) == (as_of is None):
        raise ValueError("give exactly one of before / as_of")
    op, d = ("<", before) if before is not None else ("<=", as_of)
    row = conn.execute(
        f"""SELECT * FROM equity WHERE agent_id = ? AND as_of_date {op} ?
            ORDER BY as_of_date DESC, equity_id DESC LIMIT 1""", (agent_id, d.isoformat())).fetchone()
    if row is None:
        return Account(cash=L.START_CASH)
    day = date.fromisoformat(row["as_of_date"])
    rows = conn.execute(
        """SELECT p.* FROM positions AS p WHERE p.agent_id = ? AND p.as_of_date = ?
             AND p.position_id = (SELECT MAX(q.position_id) FROM positions AS q
                                  WHERE q.agent_id = p.agent_id AND q.as_of_date = p.as_of_date
                                    AND q.symbol = p.symbol)""", (agent_id, row["as_of_date"])).fetchall()
    positions = {r["symbol"]: Position(r["qty"], r["avg_cost"]) for r in rows}
    return Account(cash=row["cash"], positions=positions, eod_date=day, eod_cash=row["cash"],
                   eod_short_value=row["short_value"], charged_to=day)


def pending_orders(conn: sqlite3.Connection, agent_id: str, created_by: str | None = None) -> list[sqlite3.Row]:
    """Ledger orders with no fill row yet, oldest first (optionally created at or before created_by)."""
    sql = """SELECT o.* FROM orders AS o
             WHERE o.agent_id = ? AND o.venue = 'ledger' AND o.corrects_id IS NULL
               AND NOT EXISTS (SELECT 1 FROM fills AS f WHERE f.order_id = o.order_id)"""
    params: list[Any] = [agent_id]
    if created_by is not None:
        sql += " AND o.created_at <= ?"
        params.append(created_by)
    return conn.execute(sql + " ORDER BY o.order_id", params).fetchall()


def day_bars(conn: sqlite3.Connection, symbols: list[str], day: date, now: datetime) -> dict[str, sqlite3.Row]:
    """Latest version, fetched at or before now, of each symbol's bar dated `day` (bar_ts <= now)."""
    if not symbols:
        return {}
    names = ", ".join(f":s{i}" for i in range(len(symbols)))
    params: dict[str, object] = {f"s{i}": s for i, s in enumerate(symbols)}
    params.update(d=day.isoformat(), now=db.utc_ts(now))
    rows = conn.execute(
        f"""SELECT p.* FROM prices AS p
            WHERE p.symbol IN ({names}) AND p.bar_date = :d AND p.bar_ts <= :now
              AND p.fetched_at = (SELECT MAX(q.fetched_at) FROM prices AS q
                                  WHERE q.symbol = p.symbol AND q.bar_date = p.bar_date
                                    AND q.fetched_at <= :now)""", params).fetchall()
    return {r["symbol"]: r for r in rows}


def ceil_minute(ts: datetime) -> datetime:
    """ts rounded up to the next whole minute (unchanged if already whole)."""
    floor = ts.replace(second=0, microsecond=0)
    return floor if floor == ts else floor + MINUTE


def fetched_window(conn: sqlite3.Connection, symbol: str, t_ref: datetime, now: datetime) -> sqlite3.Row | None:
    """fill_prices' record, known at now, that [t_ref, t_ref + WINDOW) was fetched in full for symbol."""
    return conn.execute(
        """SELECT w.* FROM crypto_fill_windows AS w
           WHERE w.window_start <= :ref AND w.window_end >= :end AND w.fetched_at <= :now
             AND EXISTS (SELECT 1 FROM json_each(w.symbols) WHERE value = :s)
           ORDER BY w.fetched_at, w.window_id LIMIT 1""",
        {"ref": db.utc_ts(t_ref), "end": db.utc_ts(t_ref + WINDOW), "now": db.utc_ts(now), "s": symbol}).fetchone()


def fill_bars(conn: sqlite3.Connection, symbols: list[str], t_ref: datetime, now: datetime,
              window_end: datetime) -> dict[str, sqlite3.Row]:
    """Per symbol, the first crypto minute bar with t_ref <= bar_ts < window_end, as known at now.

    Only bars whose minute has ended by now (bar_ts + 1 min <= now) and that were
    fetched at or before now count; of each bar, the latest such version.
    """
    out = {}
    params = {"ref": db.utc_ts(t_ref), "end": db.utc_ts(window_end), "closed_by": db.utc_ts(now - MINUTE),
              "now": db.utc_ts(now)}
    for s in symbols:
        r = conn.execute(
            """SELECT b.* FROM crypto_fill_bars AS b
               WHERE b.symbol = :s AND b.bar_ts >= :ref AND b.bar_ts < :end AND b.bar_ts <= :closed_by
                 AND b.fetched_at <= :now
                 AND b.fill_bar_id = (SELECT q.fill_bar_id FROM crypto_fill_bars AS q
                                      WHERE q.symbol = b.symbol AND q.bar_ts = b.bar_ts AND q.fetched_at <= :now
                                      ORDER BY q.fetched_at DESC, q.fill_bar_id DESC LIMIT 1)
               ORDER BY b.bar_ts LIMIT 1""", {"s": s, **params}).fetchone()
        if r is not None:
            out[s] = r
    return out


# ---------------------------------------------------------------------------
# Writing helpers
# ---------------------------------------------------------------------------

def _insert(conn: sqlite3.Connection, table: str, row: Mapping[str, Any]) -> int:
    cols = ", ".join(row)
    return conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(row))})",
                        list(row.values())).lastrowid


def write_risk_events(conn: sqlite3.Connection, agent_id: str, events: list[dict], occurred_at: datetime,
                      decision_id: int | None = None) -> list[int]:
    """Insert risk.apply_risk / margin_check events (no commit; caller owns the transaction)."""
    return [_insert(conn, "risk_events", {"agent_id": agent_id, "decision_id": decision_id,
                                          "occurred_at": db.utc_ts(occurred_at), **e}) for e in events]


def write_orders(conn: sqlite3.Connection, agent_id: str, asset_class: str, orders: list[Order],
                 created_at: datetime, decision_id: int | None = None,
                 risk_event_ids: Mapping[str, int] | None = None) -> list[int]:
    """Insert ledger orders (no commit). Margin orders take their symbol's risk_event_id."""
    ids = []
    for o in orders:
        ids.append(_insert(conn, "orders", {
            "agent_id": agent_id, "decision_id": decision_id if o.origin == "decision" else None,
            "risk_event_id": (risk_event_ids or {}).get(o.symbol) if o.origin == "margin" else None,
            "origin": o.origin, "venue": "ledger", "created_at": db.utc_ts(created_at),
            "symbol": o.symbol, "asset_class": asset_class, "side": o.side, "qty": o.qty,
            "target_weight": o.target_weight}))
    return ids


def _fill_row(order_id: int, f: L.Fill, filled_at: str, extra: Mapping[str, Any] | None = None) -> dict:
    return {"order_id": order_id, "status": "filled", "filled_at": filled_at, "qty": f.qty,
            "reference_price": f.reference_price, "slippage_bps": f.slippage_bps,
            "fill_price": f.fill_price, "fee_usd": f.fee_usd,
            "detail": json.dumps({"origin": f.origin, "slippage_usd": f.slippage_usd, **(extra or {})},
                                 sort_keys=True)}


def _begin(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise LedgerError("the caller has an open transaction; the ledger needs its own")
    conn.execute("BEGIN IMMEDIATE")


# ---------------------------------------------------------------------------
# Splits and frozen stocks (rule 11, v1.18)
# ---------------------------------------------------------------------------

def _judged(conn: sqlite3.Connection, symbol: str, after: date, day: date, now: datetime) -> bool:
    """symbol_events has a verdict for symbol with an ex-date in (after, day], known by now."""
    return conn.execute("SELECT 1 FROM symbol_events WHERE symbol = ? AND ex_date > ? AND ex_date <= ? "
                        "AND decided_at <= ? LIMIT 1",
                        (symbol, after.isoformat(), day.isoformat(), db.utc_ts(now))).fetchone() is not None


def _apply_splits(conn, agent_id: str, account: Account, pending: list[sqlite3.Row], day: date, now: datetime,
                  qty_of: dict[int, float]) -> tuple[Account, list[dict], list[tuple[sqlite3.Row, dict]]]:
    """Apply every split verdict the agent hasn't passed yet: no split_events row for it, and the
    agent's book is still pre-split (its last mark is before the ex-date, or the stock was frozen
    since the ex-date, so nothing in it could trade). Every such agent gets a split_events row,
    holding or not (qty 0), so a later settle never applies the split to post-split shares.

    Updates qty_of with the restated quantities; returns (account, applied splits, orders to
    cancel with their fill detail)."""
    out, cut = [], []
    for v in ca.split_verdicts(conn, day, now):
        sym, ex = v["symbol"], v["ex_date"]
        if conn.execute("SELECT 1 FROM split_events WHERE agent_id = ? AND symbol = ? AND ex_date = ? "
                        "AND corrects_id IS NULL", (agent_id, sym, ex)).fetchone():
            continue
        if account.eod_date is not None and ex <= account.eod_date.isoformat() and not ca.was_frozen(conn, sym, ex):
            continue
        rows = [r for r in pending if r["symbol"] == sym]
        before = account.shares(sym)
        restated = L.restate_orders(before, [Order(r["symbol"], r["side"], r["qty"], r["origin"], r["target_weight"])
                                             for r in rows], v["ratio"])
        account, p0, p1 = L.apply_split(account, sym, v["ratio"])
        orders = []
        for r, x in zip(rows, restated):
            orders.append({"order_id": r["order_id"], **x})
            if x["qty_after"] is None:
                cut.append((r, {"reason": "split_rounding", "ratio": v["ratio"], "symbol_event_id": v["symbol_event_id"],
                                "requested_qty": r["qty"], "restated": x}))
            else:
                qty_of[r["order_id"]] = x["qty_after"]
        out.append({"symbol": sym, "ex_date": ex, "symbol_event_id": v["symbol_event_id"], "ratio": v["ratio"],
                    "qty_before": before, "qty_after": p1.qty if p1 else 0.0,
                    "avg_cost_before": p0.avg_cost if p0 else None, "avg_cost_after": p1.avg_cost if p1 else None,
                    "orders": orders})
    return account, out, cut


def book_closes(conn: sqlite3.Connection, day: date, closes: Mapping[str, float], now: datetime) -> dict[str, float]:
    """closes with every frozen stock at its frozen mark (its last close before the event, v1.18):
    what the ledger values a book at while a stock is frozen."""
    out = dict(closes)
    out.update(ca.frozen_marks(conn, day, now))
    return out


# ---------------------------------------------------------------------------
# settle_day: fills at the open, accruals, mark at the close
# ---------------------------------------------------------------------------

def settle_day(conn: sqlite3.Connection, agent_id: str, day: date, now: datetime, mode: str = "live",
               trading: Any = None, delisted: Mapping[str, float] | None = None) -> DayResult:
    """Fill the agent's pending orders at day's open, accrue, and mark at day's close. See module doc."""
    agent_id = str(agent_id)
    if mode not in MODES:
        raise LedgerError(f"unknown mode {mode!r}")
    asset_class = _asset_class(conn, agent_id)
    crypto = asset_class == "crypto"
    delisted = dict(delisted or {})
    if crypto:
        if delisted:
            raise LedgerError("delisting is an equity rule (§7 rule 9); crypto agents have none")
        fill_from = cal.crypto_times(day - timedelta(days=1)).decide_at     # 00:15Z on day
        day_close = cal.crypto_times(day).close                              # 00:00Z on day + 1
    else:
        sess = cal.session(day, conn, trading)      # before the transaction: may refresh the cache
        if sess is None:
            raise LedgerError(f"{day} is not a trading day")
        fill_from, day_close = sess.open, sess.close
    open_ts, close_ts = db.utc_ts(fill_from), db.utc_ts(day_close)
    fee_rate = L.CRYPTO_FEE if crypto else 0.0

    _begin(conn)
    try:
        done = conn.execute(
            """SELECT (SELECT COUNT(*) FROM fills AS f JOIN orders AS o ON o.order_id = f.order_id
                       WHERE o.agent_id = :a AND o.venue = 'ledger'
                         AND f.filled_at >= :open AND f.filled_at <= :close)
                    + (SELECT COUNT(*) FROM equity WHERE agent_id = :a AND as_of_date = :d)""",
            {"a": agent_id, "open": open_ts, "close": close_ts, "d": day.isoformat()}).fetchone()[0]
        if done:
            conn.rollback()
            return DayResult("already_settled")

        account = load_account(conn, agent_id, before=day)
        # Equity: orders written by the open. Crypto: orders written by day's snapshot cutoff, the
        # latest the previous decision may be placed (the decision on day itself comes after it).
        pending = pending_orders(conn, agent_id, created_by=open_ts if not crypto else None)
        if crypto:
            last_ts = db.utc_ts(cal.crypto_times(day).snapshot_at)
            pending = [r for r in pending if r["created_at"] <= last_ts]
        fills_out: list[tuple[int, L.Fill, str]] = []        # (order_id, fill, filled_at)

        # Rule 9: delisted stocks close at their last price; their pending orders are cancelled.
        cancelled = []
        for row in pending:
            if row["symbol"] in delisted:
                _insert(conn, "fills", {"order_id": row["order_id"], "status": "cancelled", "filled_at": open_ts,
                                        "detail": json.dumps({"reason": "delisted"})})
                cancelled.append(row["symbol"])
        pending = [r for r in pending if r["symbol"] not in delisted]
        for sym, last in sorted(delisted.items()):
            d_orders = L.delisting_orders(account, sym, last)
            if d_orders:
                ids = write_orders(conn, agent_id, asset_class, d_orders, fill_from)
                account, fs = L.fill(account, d_orders, {sym: last})
                fills_out += [(i, f, open_ts) for i, f in zip(ids, fs)]

        # Rule 11 (v1.18): splits confirmed since the last settle are applied before the fills,
        # with the pending orders restated in post-split shares; a frozen stock is held at its
        # last close before the event and its pending orders are cancelled.
        qty_of = {r["order_id"]: r["qty"] for r in pending}
        split_out: list[dict] = []              # one per applied split, with its restated orders
        frozen_marks: dict[str, float] = {}
        if mode == "live" and not crypto:
            account, split_out, cut = _apply_splits(conn, agent_id, account, pending, day, now, qty_of)
            for row, why in cut:
                _insert(conn, "fills", {"order_id": row["order_id"], "status": "cancelled", "filled_at": open_ts,
                                        "detail": json.dumps(why, sort_keys=True)})
                cancelled.append(row["symbol"])
            frozen_marks = {s: m for s, m in ca.frozen_marks(conn, day, now).items()}
            for row in pending:
                if row["symbol"] in frozen_marks and all(row["order_id"] != c["order_id"] for c, _ in cut):
                    _insert(conn, "fills", {"order_id": row["order_id"], "status": "cancelled", "filled_at": open_ts,
                                            "detail": json.dumps({"reason": "symbol_frozen"})})
                    cancelled.append(row["symbol"])
            gone = {c["order_id"] for c, _ in cut}
            pending = [r for r in pending if r["order_id"] not in gone and r["symbol"] not in frozen_marks]

        # Rule 11: every bar needed must be stored, fetched by now, and closed.
        orders = [Order(r["symbol"], r["side"], qty_of[r["order_id"]], r["origin"], r["target_weight"])
                  for r in pending]
        held_frozen = sorted(s for s in account.positions if s in frozen_marks)
        needed = sorted((set(account.positions) | {o.symbol for o in orders}) - set(held_frozen))
        bars = day_bars(conn, needed, day, now)
        if crypto:
            is_closed = lambda r: crypto_bar_closed(cal._parse_ts(r["bar_ts"]), now)  # noqa: E731
        else:
            is_closed = lambda r: equity_bar_closed(  # noqa: E731
                cal._parse_ts(r["bar_ts"]), now, lambda d: sess.close if d == day else None)
        missing = [s for s in needed if s not in bars or not is_closed(bars[s])]
        if missing:
            raise PriceNotAvailable(f"{day}: no closed, stored bar yet for {', '.join(missing)} "
                                    f"(as of {db.utc_ts(now)})")
        if mode == "live" and not crypto and account.eod_date is not None:
            prev = day_bars(conn, needed, account.eod_date, now)
            split = [s for s, r in prev.items() if abs(r["adj_close"] / r["close"] - 1) > SPLIT_TOL
                     and not _judged(conn, s, account.eod_date, day, now)]
            if split:
                raise SplitDetected(f"price step in {', '.join(split)} between {account.eod_date} and {day} "
                                    f"with no verdict: run the corporate-actions review (data/corporate_actions.py)")
        o_key, c_key = ("open", "close") if mode == "live" else ("adj_open", "adj_close")
        closes = {s: bars[s][c_key] for s in needed}
        closes.update({s: frozen_marks[s] for s in held_frozen})
        filled_at = {s: open_ts for s in needed}
        bar_id: dict[str, int] = {}
        if crypto:
            # §7 rule 4 (v1.13): the first closed minute bar in [t_ref, t_ref + 60 min).
            t_ref = ceil_minute(max([fill_from] + [cal._parse_ts(r["created_at"]) for r in pending]))
            symbols = sorted({o.symbol for o in orders})
            fb = fill_bars(conn, symbols, t_ref, now, t_ref + WINDOW)
            gone = [s for s in symbols if s not in fb]
            windows = {s: fetched_window(conn, s, t_ref, now) for s in gone}
            waiting = [s for s in gone if windows[s] is None]
            if waiting:
                raise PriceNotAvailable(f"{day}: no closed, stored minute bar at or after {db.utc_ts(t_ref)} "
                                        f"for {', '.join(waiting)} (as of {db.utc_ts(now)})")
            for i, r in enumerate(pending):                  # no trade in the whole window: cancelled
                if r["symbol"] in windows:
                    w = windows[r["symbol"]]
                    _insert(conn, "fills", {"order_id": r["order_id"], "status": "cancelled",
                                            "filled_at": db.utc_ts(t_ref), "detail": json.dumps(
                                                {"reason": "no_fill_bar", "window_start": w["window_start"],
                                                 "window_end": w["window_end"], "window_id": w["window_id"]},
                                                sort_keys=True)})
            cancelled += [s for s in gone]
            keep = [i for i, r in enumerate(pending) if r["symbol"] not in windows]
            pending, orders = [pending[i] for i in keep], [orders[i] for i in keep]
            symbols = [s for s in symbols if s in fb]
            opens = {s: fb[s]["open"] for s in symbols}
            filled_at.update({s: fb[s]["bar_ts"] for s in symbols})
            bar_id = {s: fb[s]["fill_bar_id"] for s in symbols}
            cancel_ts = db.utc_ts(t_ref)
        else:
            opens = {s: bars[s][o_key] for s in needed}
            cancel_ts = open_ts

        # Rule 12: long-only 100%-gross agents fill sells first and never go below zero cash.
        if L.has_cash_floor(agent_id):
            plan, floor_events = L.cash_floor(account, orders, opens, fee_rate=fee_rate)
        else:
            plan, floor_events = list(enumerate(orders)), []
        scaled = [i for i, _ in plan if orders[i].side == "buy"] if floor_events else []
        event_of = dict(zip(scaled, floor_events))           # pending index -> its cash_floor event
        to_fill = [(i, o) for i, o in plan if o is not None]
        account, fs = L.fill(account, [o for _, o in to_fill], opens, fee_rate=fee_rate)
        filled = [(i, f) for (i, _), f in zip(to_fill, fs)]
        account, borrow, interest = L.accrue(account, day)
        account, mk = L.mark(account, closes, day)

        split_id = {}                                        # order_id -> split_event_id
        for sp in split_out:
            sid = _insert(conn, "split_events", {
                "agent_id": agent_id, "symbol": sp["symbol"], "ex_date": sp["ex_date"], "applied_on": day.isoformat(),
                "symbol_event_id": sp["symbol_event_id"], "ratio": sp["ratio"], "qty_before": sp["qty_before"],
                "qty_after": sp["qty_after"], "avg_cost_before": sp["avg_cost_before"],
                "avg_cost_after": sp["avg_cost_after"], "applied_at": db.utc_ts(now),
                "detail": json.dumps({"orders": sp["orders"]}, sort_keys=True)})
            sp["split_event_id"] = sid
            for o in sp["orders"]:
                split_id[o["order_id"]] = (sid, o["qty_before"], sp["ratio"])
        event_id = {i: write_risk_events(conn, agent_id, [e], fill_from, pending[i]["decision_id"])[0]
                    for i, e in event_of.items()}
        for i, o in plan:
            if o is None:
                _insert(conn, "fills", {"order_id": pending[i]["order_id"], "status": "cancelled",
                                        "filled_at": cancel_ts, "detail": json.dumps(
                                            {"reason": "cash_floor", "risk_event_id": event_id[i]}, sort_keys=True)})
        for oid, f, at in fills_out:                         # delisting fills
            _insert(conn, "fills", _fill_row(oid, f, at))
        for i, f in filled:
            extra = ({"risk_event_id": event_id[i], "requested_qty": pending[i]["qty"]} if i in event_id else {})
            if pending[i]["order_id"] in split_id:
                sid, q0, ratio = split_id[pending[i]["order_id"]]
                extra.update(split_event_id=sid, requested_qty=q0, split_ratio=ratio,
                             restated_qty=qty_of[pending[i]["order_id"]])
            if f.symbol in bar_id:
                extra["fill_bar_id"] = bar_id[f.symbol]
            _insert(conn, "fills", _fill_row(pending[i]["order_id"], f, filled_at[f.symbol], extra or None))
        fills_out += [(pending[i]["order_id"], f, filled_at[f.symbol]) for i, f in filled]
        for s, p in sorted(account.positions.items()):
            _insert(conn, "positions", {
                "agent_id": agent_id, "as_of_date": day.isoformat(), "symbol": s, "asset_class": asset_class,
                "qty": p.qty, "avg_cost": p.avg_cost, "mark_price": closes[s], "market_value": mk.values[s],
                "unrealised_pnl": (closes[s] - p.avg_cost) * p.qty})
        _insert(conn, "equity", {
            "agent_id": agent_id, "as_of_date": day.isoformat(), "cash": mk.cash, "long_value": mk.long_value,
            "short_value": mk.short_value, "equity": mk.equity, "gross_exposure": mk.gross_exposure,
            "borrow_fee_usd": borrow, "margin_interest_usd": interest,
            "trading_fees_usd": math.fsum(f.fee_usd for _, f, _ in fills_out)})
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return DayResult("settled", tuple(f for _, f, _ in fills_out), tuple(cancelled), mk, borrow, interest,
                     tuple(floor_events), tuple(split_out), tuple(sorted(frozen_marks)))


# ---------------------------------------------------------------------------
# place_orders: margin check, then the decision's orders
# ---------------------------------------------------------------------------

def place_orders(conn: sqlite3.Connection, agent_id: str, day: date, closes: Mapping[str, float],
                 targets: Mapping[str, float] | None, created_at: datetime,
                 decision_id: int | None = None, computed_at: datetime | None = None) -> Placed:
    """Turn day's (risk-checked) targets into pending orders, after the margin check (rule 8).

    closes are the snapshot closes the agent saw. targets None = hold. The
    margin check always runs and is always logged; a non-hold decision
    replaces the forced reduction (its targets already satisfy the margin rule).
    computed_at (protocol v1.17): when created_at is the protocol schedule time rather than
    now (a past-deadline day), the real computation time, recorded in each margin event's detail.
    """
    agent_id = str(agent_id)
    if targets is not None and decision_id is None:
        raise LedgerError("decision orders need a decision_id")
    if _asset_class(conn, agent_id) == "crypto" and created_at > cal.crypto_times(day + timedelta(days=1)).snapshot_at:
        return Placed("missed")      # §7 rule 4 (v1.13): after the next day's snapshot cutoff, no trade
    _begin(conn)
    try:
        if pending_orders(conn, agent_id):
            conn.rollback()
            return Placed("already_placed")
        account = load_account(conn, agent_id, as_of=day)
        if account.eod_date != day:
            raise LedgerError(f"agent {agent_id} isn't settled for {day} (last close {account.eod_date})")
        frozen = {}
        if _asset_class(conn, agent_id) == "us_equity":
            frozen = ca.frozen_marks(conn, day, computed_at or created_at)       # rule 11 (v1.18)
            closes = {**closes, **{s: m for s, m in frozen.items() if s in closes or s in account.positions}}
        _, mk = L.mark(account, closes, day)
        m_orders, m_events = L.margin_orders(account, mk, closes)
        superseded = bool(m_events) and targets is not None
        if superseded:
            m_events = [{**e, "detail": json.dumps({**json.loads(e["detail"]), "superseded_by_decision": True},
                                                   sort_keys=True)} for e in m_events]
        if computed_at is not None:
            m_events = [{**e, "detail": json.dumps({**json.loads(e["detail"]), "computed_at": db.utc_ts(computed_at)},
                                                   sort_keys=True)} for e in m_events]
        ev_ids = write_risk_events(conn, agent_id, m_events, created_at)
        if targets is not None:
            orders = L.plan_orders(account, targets, closes, origin="decision")
        else:
            orders = m_orders
        dropped = [o for o in orders if o.symbol in frozen]
        if dropped:          # a frozen stock: no new orders in it (rule 11, v1.18), each one logged
            write_risk_events(conn, agent_id, [{
                "event_type": "reject", "rule": "symbol_frozen", "symbol": o.symbol,
                "requested_weight": o.target_weight, "applied_weight": None,
                "detail": json.dumps({"side": o.side, "qty": o.qty, "origin": o.origin,
                                      "held_at": frozen[o.symbol]}, sort_keys=True)} for o in dropped],
                created_at, decision_id if targets is not None else None)
            orders = [o for o in orders if o.symbol not in frozen]
        ids = write_orders(conn, agent_id, _asset_class(conn, agent_id), orders, created_at, decision_id,
                           {e["symbol"]: i for e, i in zip(m_events, ev_ids)})
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return Placed("placed", tuple(orders), tuple(ids), tuple(m_events), superseded)
