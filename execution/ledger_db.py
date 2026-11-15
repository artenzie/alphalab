"""The ledger's database layer (protocol §7 v1.5, §9): load and store what execution/ledger.py computes.

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

Crypto (C0, C1) needs an intraday price after the 00:15Z decision, which isn't
stored yet: settle_day raises PriceNotAvailable for crypto agents.

Accounts are rebuilt from the last equity row and its positions rows; with no
rows the agent starts with START_CASH. Corrections (corrects_id) aren't
written by this module; when reading, the newest row per day and symbol wins.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping

from data import calendar as cal
from data import db
from data.ingest import equity_bar_closed
from execution import ledger as L
from execution.ledger import Account, LedgerError, Order, Position

MODES = ("live", "backtest")
SPLIT_TOL = 2e-3          # as data/checks.py RATIO_TOL: Alpaca rounds adjusted prices


class PriceNotAvailable(LedgerError):
    """A price the fill or mark needs isn't stored and closed yet (ledger rule 11)."""


class SplitDetected(LedgerError):
    """A split between the last close and now: the ledger stops for a manual correction."""


@dataclass(frozen=True)
class DayResult:
    status: str                       # 'settled' | 'already_settled'
    fills: tuple[L.Fill, ...] = ()
    cancelled: tuple[str, ...] = ()   # symbols whose pending orders were cancelled (delisted)
    mark: L.Mark | None = None
    borrow: float = 0.0
    interest: float = 0.0


@dataclass(frozen=True)
class Placed:
    status: str                       # 'placed' | 'already_placed'
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


def _fill_row(order_id: int, f: L.Fill, filled_at: str) -> dict:
    return {"order_id": order_id, "status": "filled", "filled_at": filled_at, "qty": f.qty,
            "reference_price": f.reference_price, "slippage_bps": f.slippage_bps,
            "fill_price": f.fill_price, "fee_usd": f.fee_usd,
            "detail": json.dumps({"origin": f.origin, "slippage_usd": f.slippage_usd}, sort_keys=True)}


def _begin(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise LedgerError("the caller has an open transaction; the ledger needs its own")
    conn.execute("BEGIN IMMEDIATE")


# ---------------------------------------------------------------------------
# settle_day: fills at the open, accruals, mark at the close
# ---------------------------------------------------------------------------

def settle_day(conn: sqlite3.Connection, agent_id: str, day: date, now: datetime, mode: str = "live",
               trading: Any = None, delisted: Mapping[str, float] | None = None) -> DayResult:
    """Fill the agent's pending orders at day's open, accrue, and mark at day's close. See module doc."""
    agent_id = str(agent_id)
    if mode not in MODES:
        raise LedgerError(f"unknown mode {mode!r}")
    if _asset_class(conn, agent_id) == "crypto":
        raise PriceNotAvailable("crypto fills need an intraday price after the decision; source not built yet")
    sess = cal.session(day, conn, trading)          # before the transaction: may refresh the cache
    if sess is None:
        raise LedgerError(f"{day} is not a trading day")
    open_ts, close_ts = db.utc_ts(sess.open), db.utc_ts(sess.close)
    delisted = dict(delisted or {})

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
        pending = pending_orders(conn, agent_id, created_by=open_ts)
        fills_out: list[tuple[int, L.Fill]] = []

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
                ids = write_orders(conn, agent_id, "us_equity", d_orders, sess.open)
                account, fs = L.fill(account, d_orders, {sym: last})
                fills_out += list(zip(ids, fs))

        # Rule 11: every bar needed must be stored, fetched by now, and closed.
        orders = [Order(r["symbol"], r["side"], r["qty"], r["origin"], r["target_weight"]) for r in pending]
        needed = sorted(set(account.positions) | {o.symbol for o in orders})
        bars = day_bars(conn, needed, day, now)
        missing = [s for s in needed if s not in bars
                   or not equity_bar_closed(cal._parse_ts(bars[s]["bar_ts"]), now,
                                            lambda d: sess.close if d == day else None)]
        if missing:
            raise PriceNotAvailable(f"{day}: no closed, stored bar yet for {', '.join(missing)} "
                                    f"(as of {db.utc_ts(now)})")
        if mode == "live" and account.eod_date is not None:
            prev = day_bars(conn, needed, account.eod_date, now)
            split = [s for s, r in prev.items() if abs(r["adj_close"] / r["close"] - 1) > SPLIT_TOL]
            if split:
                raise SplitDetected(f"split in {', '.join(split)} between {account.eod_date} and {day}: "
                                    f"correct the positions by hand before settling")
        o_key, c_key = ("open", "close") if mode == "live" else ("adj_open", "adj_close")
        opens = {s: bars[s][o_key] for s in needed}
        closes = {s: bars[s][c_key] for s in needed}

        account, fs = L.fill(account, orders, opens)
        fills_out += [(r["order_id"], f) for r, f in zip(pending, fs)]
        account, borrow, interest = L.accrue(account, day)
        account, mk = L.mark(account, closes, day)

        for oid, f in fills_out:
            _insert(conn, "fills", _fill_row(oid, f, open_ts))
        for s, p in sorted(account.positions.items()):
            _insert(conn, "positions", {
                "agent_id": agent_id, "as_of_date": day.isoformat(), "symbol": s, "asset_class": "us_equity",
                "qty": p.qty, "avg_cost": p.avg_cost, "mark_price": closes[s], "market_value": mk.values[s],
                "unrealised_pnl": (closes[s] - p.avg_cost) * p.qty})
        _insert(conn, "equity", {
            "agent_id": agent_id, "as_of_date": day.isoformat(), "cash": mk.cash, "long_value": mk.long_value,
            "short_value": mk.short_value, "equity": mk.equity, "gross_exposure": mk.gross_exposure,
            "borrow_fee_usd": borrow, "margin_interest_usd": interest,
            "trading_fees_usd": math.fsum(f.fee_usd for _, f in fills_out)})
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return DayResult("settled", tuple(f for _, f in fills_out), tuple(cancelled), mk, borrow, interest)


# ---------------------------------------------------------------------------
# place_orders: margin check, then the decision's orders
# ---------------------------------------------------------------------------

def place_orders(conn: sqlite3.Connection, agent_id: str, day: date, closes: Mapping[str, float],
                 targets: Mapping[str, float] | None, created_at: datetime,
                 decision_id: int | None = None) -> Placed:
    """Turn day's (risk-checked) targets into pending orders, after the margin check (rule 8).

    closes are the snapshot closes the agent saw. targets None = hold. The
    margin check always runs and is always logged; a non-hold decision
    replaces the forced reduction (its targets already satisfy the margin rule).
    """
    agent_id = str(agent_id)
    if targets is not None and decision_id is None:
        raise LedgerError("decision orders need a decision_id")
    _begin(conn)
    try:
        if pending_orders(conn, agent_id):
            conn.rollback()
            return Placed("already_placed")
        account = load_account(conn, agent_id, as_of=day)
        if account.eod_date != day:
            raise LedgerError(f"agent {agent_id} isn't settled for {day} (last close {account.eod_date})")
        _, mk = L.mark(account, closes, day)
        m_orders, m_events = L.margin_orders(account, mk, closes)
        superseded = bool(m_events) and targets is not None
        if superseded:
            m_events = [{**e, "detail": json.dumps({**json.loads(e["detail"]), "superseded_by_decision": True},
                                                   sort_keys=True)} for e in m_events]
        ev_ids = write_risk_events(conn, agent_id, m_events, created_at)
        if targets is not None:
            orders = L.plan_orders(account, targets, closes, origin="decision")
        else:
            orders = m_orders
        ids = write_orders(conn, agent_id, _asset_class(conn, agent_id), orders, created_at, decision_id,
                           {e["symbol"]: i for e, i in zip(m_events, ev_ids)})
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return Placed("placed", tuple(orders), tuple(ids), tuple(m_events), superseded)
