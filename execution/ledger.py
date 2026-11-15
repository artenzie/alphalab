"""The simulated ledger's accounting core (protocol §7 v1.8, ledger rules 1-12): pure functions.

    orders = plan_orders(account, targets, closes)              # decision day D, at D's close
    account, fills = fill(account, orders, opens)               # D+1, at the official open
    account, borrow, interest = accrue(account, day)            # D+1 close: days since the last close
    account, mk = mark(account, closes, day)                    # D+1 close: mark to market
    orders, events = margin_orders(account, mk, closes)         # D+1 close, before the decision

No database, network, file or environment access: prices, targets and dates
come in as arguments, and every function returns new objects. The database
layer (execution/ledger_db.py) loads and stores accounts and runs the day.

Rules, as written in protocol §7:
  1. Target shares = weight x portfolio value at the decision close / that close,
     with exactly the closes the agent saw. Order = target - current shares.
     Non-hold targets are the complete portfolio: a held symbol they leave out
     has target 0 (v1.8). A hold (None) trades nothing.
  2. An order worth under $100 at the decision close (the whole change) is skipped.
  3. Long targets are rounded down to 9 decimals of a share, short targets
     towards zero to whole shares. A flip is two orders: close, then open.
  4. Fills at the reference price (equity: next official open) with 5 bps
     slippage against the agent; crypto adds a 0.25% fee (fee_rate=CRYPTO_FEE).
  5. Short proceeds go to cash; cash earns nothing.
  6. Every calendar day is charged, on a 360-day year, in arrears: at each close
     the days since the previous close are charged on that previous close's
     balances (borrow 0.50%/yr on short value, interest 6.5%/yr on negative
     cash). Monday's close charges Fri, Sat and Sun on Friday's balances. The
     run's last close is therefore never charged for the days after it.
  7. Equity = cash + sum(shares x close). Dividends are ignored.
  8. margin_orders turns risk.margin_check's forced reduction into orders.
  9. delisting_orders closes a position at its last traded price (slippage
     applies, as on every fill; the $100 minimum does not, so nothing is left
     without a price).
 12. cash_floor (agents with has_cash_floor: long only, 100% gross limit)
     puts the day's sells first and scales all buys down pro rata so the fills
     never take cash below 0. Each scaled buy is a 'cash_floor' risk event.
     Scaled buys are filled even under $100 (the minimum is a decision-time rule),
     but a buy scaled to under MIN_FLOOR_BUY_USD ($1) is cancelled.

Money is kept as unrounded floats; the accounting identity (change in equity
= price P&L - slippage - fees - borrow - interest) holds to well under a cent.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_DOWN, Decimal
from types import MappingProxyType
from typing import Mapping

from execution.risk import TOL, margin_check, profile

START_CASH = 100_000.0
SLIPPAGE_BPS = 5.0
MIN_TRADE_USD = 100.0
BORROW_RATE = 0.005        # per year, on short market value
MARGIN_RATE = 0.065        # per year, on negative cash
DAY_COUNT = 360
CRYPTO_FEE = 0.0025        # §13: Alpaca tier-1 taker fee
SHARE_DP = 9               # Alpaca fractional precision
EPS_USD = 1e-9             # float noise allowed in the $100 test
FLOOR_HAIRCUT = 1e-12      # scale buys a hair under exact, so float noise can't end below $0
MIN_FLOOR_BUY_USD = 1.0    # rule 12: a buy scaled to under this (at the fill price) is cancelled

ORIGINS = ("decision", "margin", "delisting")


class LedgerError(ValueError):
    """Inputs the ledger can't account for (missing price, bad quantity, ...)."""


@dataclass(frozen=True)
class Position:
    qty: float           # shares, negative = short
    avg_cost: float      # average fill price of the open side, slippage included


@dataclass(frozen=True)
class Account:
    """An agent's book. eod_* are the balances at the last close (for accruals)."""
    cash: float = START_CASH
    positions: Mapping[str, Position] = field(default_factory=dict)
    eod_date: date | None = None         # date of the last mark
    eod_cash: float = 0.0
    eod_short_value: float = 0.0         # positive number
    charged_to: date | None = None       # calendar days before this are charged

    def __post_init__(self) -> None:
        clean = {s: p for s, p in dict(self.positions).items() if p.qty != 0}
        object.__setattr__(self, "positions", MappingProxyType(clean))

    def shares(self, symbol: str) -> float:
        p = self.positions.get(symbol)
        return p.qty if p else 0.0

    def value(self, closes: Mapping[str, float]) -> float:
        """cash + sum(shares x close)."""
        return self.cash + math.fsum(p.qty * _price(closes, s) for s, p in self.positions.items())


@dataclass(frozen=True)
class Order:
    symbol: str
    side: str                      # 'buy' | 'sell'
    qty: float                     # > 0
    origin: str = "decision"       # 'decision' | 'margin' | 'delisting'
    target_weight: float | None = None
    decision_close: float | None = None   # the close it was sized at

    def __post_init__(self) -> None:
        if self.side not in ("buy", "sell"):
            raise LedgerError(f"bad side {self.side!r}")
        if not (isinstance(self.qty, (int, float)) and math.isfinite(self.qty) and self.qty > 0):
            raise LedgerError(f"order qty must be a positive number, got {self.qty!r}")
        if self.origin not in ORIGINS:
            raise LedgerError(f"bad origin {self.origin!r}")

    @property
    def signed_qty(self) -> float:
        return self.qty if self.side == "buy" else -self.qty


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: str
    qty: float
    origin: str
    reference_price: float
    fill_price: float
    slippage_bps: float
    slippage_usd: float
    fee_usd: float

    @property
    def signed_qty(self) -> float:
        return self.qty if self.side == "buy" else -self.qty


@dataclass(frozen=True)
class Mark:
    as_of: date
    cash: float
    long_value: float
    short_value: float          # positive number
    equity: float
    gross_exposure: float
    values: Mapping[str, float]  # symbol -> signed market value
    closes: Mapping[str, float]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _price(prices: Mapping[str, float], symbol: str) -> float:
    p = prices.get(symbol)
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or p <= 0:
        raise LedgerError(f"no valid price for {symbol}: {p!r}")
    return float(p)


def round_long(qty: float) -> float:
    """Round a long share count down to SHARE_DP decimals (exact decimal, no float drift)."""
    return float(Decimal(repr(qty)).quantize(Decimal(1).scaleb(-SHARE_DP), rounding=ROUND_DOWN))


def target_shares(weight: float, pv: float, close: float) -> float:
    """Rule 1 + 3: shares for a weight, longs down to 9 dp, shorts towards zero to whole shares."""
    raw = weight * pv / close
    if raw > 0:
        return round_long(raw)
    if raw < 0:
        return float(math.trunc(raw))
    return 0.0


def _q(x: float) -> float:
    """Remove float noise from a share count (shares are never finer than 9 dp)."""
    return round(x, SHARE_DP) + 0.0      # + 0.0 turns -0.0 into 0.0


def _side(delta: float) -> str:
    return "buy" if delta > 0 else "sell"


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def plan_orders(account: Account, targets: Mapping[str, float] | None, closes: Mapping[str, float],
                origin: str = "decision", min_trade: float = MIN_TRADE_USD) -> list[Order]:
    """Turn target weights into share orders at the decision close (rules 1-3).

    targets are the complete target portfolio: a held symbol not in targets
    has weight 0. closes must have every held symbol and every target symbol.
    Orders come out by symbol, a flip as its two legs (close first).
    """
    if targets is None:
        return []
    pv = account.value(closes)
    orders: list[Order] = []
    held = {s for s, p in account.positions.items() if p.qty != 0}
    for s in sorted(set(targets) | held):
        w = targets.get(s, 0.0)
        if isinstance(w, bool) or not isinstance(w, (int, float)) or not math.isfinite(w):
            raise LedgerError(f"target weight for {s} must be a finite number, got {w!r}")
        close = _price(closes, s)
        cur = account.shares(s)
        tgt = target_shares(float(w), pv, close)
        delta = _q(tgt - cur)
        if delta == 0 or abs(delta) * close < min_trade - EPS_USD:
            continue
        common = dict(origin=origin, target_weight=float(w), decision_close=close)
        if cur != 0 and tgt != 0 and (cur > 0) != (tgt > 0):          # flip: close, then open
            orders.append(Order(s, _side(-cur), abs(cur), **common))
            orders.append(Order(s, _side(tgt), abs(tgt), **common))
        else:
            orders.append(Order(s, _side(delta), abs(delta), **common))
    return orders


def margin_orders(account: Account, mk: Mark, closes: Mapping[str, float]) -> tuple[list[Order], list[dict]]:
    """Rule 8: risk.margin_check on the mark, as orders. ([], []) when there is no breach."""
    weights, events = margin_check(mk.equity, mk.gross_exposure, dict(mk.values))
    if weights is None:
        return [], []
    return plan_orders(account, weights, closes, origin="margin"), events


def delisting_orders(account: Account, symbol: str, last_price: float) -> list[Order]:
    """Rule 9: close the whole position (no $100 minimum)."""
    qty = account.shares(symbol)
    if qty == 0:
        return []
    price = _price({symbol: last_price}, symbol)
    return [Order(symbol, _side(-qty), abs(qty), origin="delisting", target_weight=0.0, decision_close=price)]


def has_cash_floor(agent_id: int | str) -> bool:
    """Rule 12 applies: the agent can't short and its gross limit is 100% (1, 2, 3, 5, 7, 14, C0, C1)."""
    p = profile(agent_id)
    return not p.shorts and p.max_gross <= 1 + TOL


def cash_floor(account: Account, orders: list[Order], ref_prices: Mapping[str, float],
               slippage_bps: float = SLIPPAGE_BPS, fee_rate: float = 0.0
               ) -> tuple[list[tuple[int, Order | None]], list[dict]]:
    """Rule 12: sells first, then buys scaled pro rata so cash can't end below 0.

    Returns (plan, events). plan is [(index into orders, order to fill or None)]
    in fill order: sells as given, then buys, each scaled by the same factor k
    (shares rounded down; None when its scaled value at the fill price,
    slippage and fee included, is under MIN_FLOOR_BUY_USD). events has one
    risk_events-shaped dict per scaled buy (empty when nothing is scaled).
    applied_weight is the target weight x k, an approximation; the exact
    quantities are in detail. A cancelled buy has applied_qty and
    applied_weight 0 and says why in detail['cancelled']; k is not
    recomputed, so the cash it would have used stays as cash.
    """
    bps = slippage_bps / 10_000
    sells = [(i, o) for i, o in enumerate(orders) if o.side == "sell"]
    buys = [(i, o) for i, o in enumerate(orders) if o.side == "buy"]
    cash_after_sells = account.cash + math.fsum(
        o.qty * _price(ref_prices, o.symbol) * (1 - bps) * (1 - fee_rate) for _, o in sells)
    cost = math.fsum(o.qty * _price(ref_prices, o.symbol) * (1 + bps) * (1 + fee_rate) for _, o in buys)
    plan: list[tuple[int, Order | None]] = list(sells)
    if not buys or cost <= cash_after_sells:
        return plan + list(buys), []
    k = max(0.0, cash_after_sells) / cost * (1 - FLOOR_HAIRCUT)
    events = []
    for i, o in buys:
        q = round_long(o.qty * k)
        value = q * _price(ref_prices, o.symbol) * (1 + bps) * (1 + fee_rate)
        cancel = value < MIN_FLOOR_BUY_USD
        detail = {"requested_qty": o.qty, "applied_qty": 0.0 if cancel else q, "factor": k,
                  "cash_after_sells": cash_after_sells, "buy_cost": cost,
                  "applied_weight_is": "target_weight x factor (approximate)"}
        if cancel:
            detail.update(cancelled=f"scaled value under ${MIN_FLOOR_BUY_USD:g}", scaled_qty=q, scaled_value=value)
        plan.append((i, None if cancel else replace(o, qty=q)))
        events.append({
            "event_type": "clip", "rule": "cash_floor", "symbol": o.symbol,
            "requested_weight": o.target_weight,
            "applied_weight": None if o.target_weight is None else (0.0 if cancel else o.target_weight * k),
            "detail": json.dumps(detail, sort_keys=True)})
    return plan, events


# ---------------------------------------------------------------------------
# Fills, accruals, marks
# ---------------------------------------------------------------------------

def fill(account: Account, orders: list[Order], ref_prices: Mapping[str, float],
         slippage_bps: float = SLIPPAGE_BPS, fee_rate: float = 0.0) -> tuple[Account, list[Fill]]:
    """Fill orders in sequence at ref price +/- slippage (rules 4, 5)."""
    bps = slippage_bps / 10_000
    cash = account.cash
    pos = dict(account.positions)
    fills: list[Fill] = []
    for o in orders:
        ref = _price(ref_prices, o.symbol)
        price = ref * (1 + bps) if o.side == "buy" else ref * (1 - bps)
        fee = fee_rate * o.qty * price
        cash -= o.signed_qty * price + fee
        old = pos.get(o.symbol, Position(0.0, 0.0))
        new_qty = _q(old.qty + o.signed_qty)
        if new_qty == 0:
            pos.pop(o.symbol, None)
        else:
            if old.qty == 0 or (old.qty > 0) != (new_qty > 0):          # opened or flipped
                avg = price
            elif abs(new_qty) > abs(old.qty):                           # grew
                avg = (abs(old.qty) * old.avg_cost + o.qty * price) / abs(new_qty)
            else:                                                       # shrank
                avg = old.avg_cost
            pos[o.symbol] = Position(new_qty, avg)
        fills.append(Fill(o.symbol, o.side, o.qty, o.origin, ref, price, slippage_bps,
                          o.qty * ref * bps, fee))
    return replace(account, cash=cash, positions=pos), fills


def accrue(account: Account, day: date, borrow_rate: float = BORROW_RATE,
           margin_rate: float = MARGIN_RATE) -> tuple[Account, float, float]:
    """Rule 6: charge each calendar day in [charged_to, day) on the last close's balances.

    Returns (account, borrow_fee, margin_interest). Charging the same day twice is a no-op.
    """
    if account.charged_to is None or account.eod_date is None:
        return replace(account, charged_to=day), 0.0, 0.0
    n = (day - account.charged_to).days
    if n < 0:
        raise LedgerError(f"accrual for {day} is before {account.charged_to}, already charged")
    borrow = n * borrow_rate / DAY_COUNT * account.eod_short_value
    interest = n * margin_rate / DAY_COUNT * max(0.0, -account.eod_cash)
    return replace(account, cash=account.cash - borrow - interest, charged_to=day), borrow, interest


def mark(account: Account, closes: Mapping[str, float], day: date) -> tuple[Account, Mark]:
    """Rule 7: mark to market at the close; records the day's balances for tomorrow's accrual."""
    values = {s: p.qty * _price(closes, s) for s, p in sorted(account.positions.items())}
    long_value = math.fsum(v for v in values.values() if v > 0)
    short_value = -math.fsum(v for v in values.values() if v < 0)
    mk = Mark(day, account.cash, long_value, short_value, account.cash + long_value - short_value,
              long_value + short_value, MappingProxyType(values),
              MappingProxyType({s: _price(closes, s) for s in values}))
    acct = replace(account, eod_date=day, eod_cash=account.cash, eod_short_value=short_value,
                   charged_to=account.charged_to or day)
    return acct, mk
