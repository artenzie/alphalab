"""Ledger accounting core (protocol §7 v1.5): execution/ledger.py with small hand-calculated numbers.

Each case states its arithmetic in a comment. The last tests check the
accounting identity over many random days, and that the module is pure.
"""

import ast
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from execution import ledger as L  # noqa: E402
from execution.ledger import Account, Order, Position  # noqa: E402
from test_strategies import sealed  # noqa: E402

FRI, SAT, MON, TUE = date(2026, 11, 13), date(2026, 11, 14), date(2026, 11, 16), date(2026, 11, 17)


def acct(cash, **pos):
    return Account(cash=cash, positions={s: Position(q, 0.0) for s, q in pos.items()})


def legs(orders):
    return [(o.symbol, o.side, o.qty) for o in orders]


def marked(cash, day, **pos_and_close):
    """An account marked at `day`; kwargs are symbol=(qty, close)."""
    a = Account(cash=cash, positions={s: Position(q, c) for s, (q, c) in pos_and_close.items()})
    a = L.accrue(a, day)[0]
    return L.mark(a, {s: c for s, (q, c) in pos_and_close.items()}, day)


# ---------------------------------------------------------------------------
# Orders (rules 1-3)
# ---------------------------------------------------------------------------

def test_buy_sized_at_decision_close_and_filled_at_open_with_slippage():
    a = acct(10_000)
    orders = L.plan_orders(a, {"A": 0.5}, {"A": 50.0})      # 0.5 * 10000 / 50 = 100 shares
    assert legs(orders) == [("A", "buy", 100.0)]
    assert orders[0].decision_close == 50.0 and orders[0].target_weight == 0.5
    a, (f,) = L.fill(a, orders, {"A": 52.0})                # 52 * 1.0005 = 52.026
    assert f.fill_price == pytest.approx(52.026)
    assert f.slippage_usd == pytest.approx(2.6)             # 100 * 52 * 0.0005
    assert a.cash == pytest.approx(10_000 - 5_202.6)
    assert a.positions["A"] == Position(100.0, pytest.approx(52.026))


def test_sell_receives_less():
    a = acct(0, A=100)
    orders = L.plan_orders(a, {"A": 0.2}, {"A": 50.0})      # pv 5000 -> 20 shares -> sell 80
    assert legs(orders) == [("A", "sell", 80.0)]
    a, (f,) = L.fill(a, orders, {"A": 40.0})                # 40 * 0.9995 = 39.98
    assert f.fill_price == pytest.approx(39.98) and f.fill_price < f.reference_price
    assert a.cash == pytest.approx(3_198.4)
    assert a.shares("A") == 20


def test_short_proceeds_go_to_cash_whole_shares():
    a = acct(10_000)
    orders = L.plan_orders(a, {"A": -0.25}, {"A": 30.0})    # -2500 / 30 = -83.33 -> -83
    assert legs(orders) == [("A", "sell", 83.0)]
    a, _ = L.fill(a, orders, {"A": 30.0})                   # 83 * 29.985 = 2488.755
    assert a.cash == pytest.approx(12_488.755)
    assert a.positions["A"] == Position(-83.0, pytest.approx(29.985))


def test_cover_pays_more():
    a = Account(cash=12_488.755, positions={"A": Position(-83.0, 29.985)})
    orders = L.plan_orders(a, {"A": 0.0}, {"A": 31.0})
    assert legs(orders) == [("A", "buy", 83.0)]
    a, (f,) = L.fill(a, orders, {"A": 31.0})                # 83 * 31.0155 = 2574.2865
    assert f.fill_price > f.reference_price
    assert a.cash == pytest.approx(12_488.755 - 2_574.2865)
    assert "A" not in a.positions


def test_flip_long_to_short_is_sell_long_then_open_short():
    a = acct(0, A=10)                                       # pv 1000 at 100
    orders = L.plan_orders(a, {"A": -0.5}, {"A": 100.0})    # -500 / 100 = -5
    assert legs(orders) == [("A", "sell", 10.0), ("A", "sell", 5.0)]
    a, _ = L.fill(a, orders, {"A": 100.0})
    assert a.positions["A"] == Position(-5.0, pytest.approx(99.95))   # avg reset on the flip
    assert a.cash == pytest.approx(15 * 99.95)


def test_flip_short_to_long_is_cover_then_buy():
    a = acct(2_000, A=-10)                                  # pv 2000 - 1000 = 1000
    orders = L.plan_orders(a, {"A": 0.5}, {"A": 100.0})     # 5 shares long
    assert legs(orders) == [("A", "buy", 10.0), ("A", "buy", 5.0)]
    a, _ = L.fill(a, orders, {"A": 100.0})
    assert a.positions["A"] == Position(5.0, pytest.approx(100.05))


def test_minimum_trade_is_100_dollars_at_the_decision_close():
    a = acct(10_000)
    assert L.plan_orders(a, {"A": 0.009999}, {"A": 1.0}) == []          # $99.99: skipped
    assert legs(L.plan_orders(a, {"A": 0.01}, {"A": 1.0})) == [("A", "buy", 100.0)]   # $100: traded


def test_fractional_long_rounded_down_to_9_decimals():
    orders = L.plan_orders(acct(1_000), {"A": 1.0}, {"A": 300.0})   # 3.3333333333...
    assert orders[0].qty == 3.333333333
    assert L.round_long(2.9999999999) == 2.999999999


def test_short_rounded_towards_zero_and_unchanged_short_no_order():
    assert legs(L.plan_orders(acct(1_000), {"A": -0.107}, {"A": 10.0})) == [("A", "sell", 10.0)]  # -10.7 -> -10
    a = acct(1_100, A=-10)                                  # pv 1100 - 100 = 1000
    assert L.plan_orders(a, {"A": -0.107}, {"A": 10.0}) == []
    assert L.target_shares(-0.9999, 1.0, 1.0) == 0.0        # a fraction of a short is no short


def test_unchanged_weight_creates_no_trade():
    a = acct(860, A=7)                                      # pv 860 + 140 = 1000, weight 0.14
    assert L.plan_orders(a, {"A": 0.14}, {"A": 20.0}) == []
    a = acct(0, A=1 / 3, B=2 / 3)
    closes = {"A": 30.0, "B": 7.0}
    pv = a.value(closes)
    w = {s: a.shares(s) * closes[s] / pv for s in closes}
    assert L.plan_orders(a, w, closes) == []


def test_hold_and_untargeted_symbols_keep_their_shares():
    a = acct(1_000, A=5)
    assert L.plan_orders(a, None, {"A": 10.0}) == []
    assert legs(L.plan_orders(a, {"B": 0.5}, {"A": 10.0, "B": 5.0})) == [("B", "buy", 105.0)]  # pv 1050


def test_missing_price_and_bad_weight_raise():
    with pytest.raises(L.LedgerError):
        L.plan_orders(acct(1_000, A=1), {"B": 0.1}, {"B": 5.0})          # held A has no close
    with pytest.raises(L.LedgerError):
        L.plan_orders(acct(1_000), {"A": float("nan")}, {"A": 5.0})
    with pytest.raises(L.LedgerError):
        L.fill(acct(1_000), [Order("A", "buy", 1.0)], {"A": 0.0})
    with pytest.raises(L.LedgerError):
        Order("A", "buy", -1.0)


def test_average_cost_grows_weighted_and_stays_when_shrinking():
    a = Account(cash=0, positions={"A": Position(10.0, 100.0)})
    a, _ = L.fill(a, [Order("A", "buy", 10.0)], {"A": 110.0}, slippage_bps=0)
    assert a.positions["A"].avg_cost == pytest.approx(105.0)
    a, _ = L.fill(a, [Order("A", "sell", 5.0)], {"A": 200.0}, slippage_bps=0)
    assert a.positions["A"] == Position(15.0, pytest.approx(105.0))


# ---------------------------------------------------------------------------
# Accruals and marks (rules 6, 7)
# ---------------------------------------------------------------------------

def test_borrow_fee_over_a_weekend_is_three_days_on_fridays_short():
    a, mk = marked(15_000, FRI, A=(-100.0, 50.0))           # short value 5000
    assert mk.short_value == 5_000 and mk.equity == 10_000
    a2, borrow, interest = L.accrue(a, MON)                 # Fri, Sat, Sun
    assert borrow == pytest.approx(3 * 0.005 / 360 * 5_000)    # 0.208333...
    assert interest == 0
    assert a2.cash == pytest.approx(15_000 - 0.2083333333)
    assert L.accrue(a2, MON)[1:] == (0.0, 0.0)               # charging the same day twice: nothing


def test_margin_interest_on_negative_cash_360_day_year():
    a, _ = marked(-36_000, MON, A=(500.0, 100.0))
    _, borrow, interest = L.accrue(a, TUE)
    assert interest == pytest.approx(6.5)                   # 36000 * 0.065 / 360
    assert borrow == 0


def test_saturday_close_and_backwards_accrual():
    a, _ = marked(-3_600, FRI, A=(100.0, 100.0))
    _, _, interest = L.accrue(a, SAT)
    assert interest == pytest.approx(0.65)
    with pytest.raises(L.LedgerError):
        L.accrue(L.accrue(a, MON)[0], SAT)


def test_first_day_charges_nothing():
    a, borrow, interest = L.accrue(Account(cash=-1e6), MON)
    assert (a.cash, borrow, interest, a.charged_to) == (-1e6, 0.0, 0.0, MON)


def test_mark_equity_long_short_gross():
    _, mk = marked(1_000, MON, A=(10.0, 20.0), B=(-5.0, 40.0))
    assert (mk.long_value, mk.short_value, mk.equity, mk.gross_exposure) == (200, 200, 1_000, 400)
    assert dict(mk.values) == {"A": 200, "B": -200}


# ---------------------------------------------------------------------------
# Margin (rule 8), delisting (rule 9), crypto
# ---------------------------------------------------------------------------

def test_margin_call_cuts_pro_rata_to_40_percent():
    a, mk = marked(-800, MON, A=(100.0, 10.0))              # equity 200, gross 1000: 20% < 30%
    orders, events = L.margin_orders(a, mk, {"A": 10.0})
    # factor 200 / (0.4 * 1000) = 0.5 -> 500 of A -> 50 shares -> sell 50
    assert legs(orders) == [("A", "sell", 50.0)]
    assert orders[0].origin == "margin"
    assert [e["event_type"] for e in events] == ["forced_reduction"]
    a, _ = L.fill(a, orders, {"A": 10.0}, slippage_bps=0)
    _, mk = L.mark(a, {"A": 10.0}, MON)
    assert mk.equity / mk.gross_exposure == pytest.approx(0.40)


def test_no_margin_orders_without_a_breach():
    a, mk = marked(-500, MON, A=(100.0, 10.0))              # 500 / 1000 = 50%
    assert L.margin_orders(a, mk, {"A": 10.0}) == ([], [])


def test_delisting_closes_at_last_price_and_cash_stays_cash():
    a = acct(100, A=10)
    orders = L.delisting_orders(a, "A", 20.0)
    assert legs(orders) == [("A", "sell", 10.0)] and orders[0].origin == "delisting"
    a, _ = L.fill(a, orders, {"A": 20.0})                   # 10 * 19.99
    assert a.cash == pytest.approx(299.9) and a.positions == {}
    assert L.delisting_orders(acct(100, A=0.5), "A", 20.0)[0].qty == 0.5   # below $100: still closed
    assert L.delisting_orders(acct(100), "A", 20.0) == []


def test_crypto_fee_and_slippage():
    a, (f,) = L.fill(acct(1_000), [Order("BTC/USD", "buy", 1.0)], {"BTC/USD": 100.0}, fee_rate=L.CRYPTO_FEE)
    assert f.fill_price == pytest.approx(100.05)
    assert f.fee_usd == pytest.approx(0.250125)             # 0.25% of 100.05
    assert a.cash == pytest.approx(1_000 - 100.300125)
    a, (f,) = L.fill(a, [Order("BTC/USD", "sell", 1.0)], {"BTC/USD": 100.0}, fee_rate=L.CRYPTO_FEE)
    assert a.cash == pytest.approx(1_000 - 100.300125 + 99.95 - 0.249875)


# ---------------------------------------------------------------------------
# Accounting identity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(5))
def test_equity_change_equals_pnl_minus_costs_to_the_cent(seed):
    rng = random.Random(seed)
    syms = ["A", "B", "C", "D"]
    close = {s: rng.uniform(5, 500) for s in syms}
    a = Account(cash=100_000.0)
    day = date(2027, 1, 4)
    a = L.accrue(a, day)[0]
    a, mk = L.mark(a, close, day)
    orders: list[Order] = []
    totals = dict(slip=0.0, fee=0.0, borrow=0.0, interest=0.0, n_fills=0)
    for _ in range(100):
        day += timedelta(days=rng.choice((1, 1, 1, 3, 4)))
        prev_close, prev_qty, prev_equity = dict(close), {s: a.shares(s) for s in syms}, mk.equity
        opens = {s: c * math.exp(rng.gauss(0, 0.02)) for s, c in close.items()}
        close = {s: o * math.exp(rng.gauss(0, 0.02)) for s, o in opens.items()}
        fee_rate = rng.choice((0.0, L.CRYPTO_FEE))
        a, fills = L.fill(a, orders, opens, fee_rate=fee_rate)
        a, borrow, interest = L.accrue(a, day)
        a, mk = L.mark(a, close, day)
        pnl = math.fsum([prev_qty[s] * (close[s] - prev_close[s]) for s in syms]
                        + [f.signed_qty * (close[f.symbol] - f.reference_price) for f in fills])
        slip = math.fsum(f.slippage_usd for f in fills)
        fees = math.fsum(f.fee_usd for f in fills)
        assert mk.equity - prev_equity == pytest.approx(pnl - slip - fees - borrow - interest, abs=0.005)
        totals["slip"] += slip
        totals["fee"] += fees
        totals["borrow"] += borrow
        totals["interest"] += interest
        totals["n_fills"] += len(fills)
        # next decision: random targets, gross up to 1.6, so cash goes negative and shorts appear
        targets = {s: rng.uniform(-0.4, 0.6) for s in rng.sample(syms, rng.randint(0, 4))}
        orders = L.plan_orders(a, targets, close)
        if rng.random() < 0.1:
            m_orders, _ = L.margin_orders(a, mk, close)
            orders = orders or m_orders
    # the test exercised every cost
    assert totals["n_fills"] > 50 and all(totals[k] > 0 for k in ("slip", "fee", "borrow", "interest"))


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------

def test_ledger_core_imports_nothing_external():
    tree = ast.parse((ROOT / "execution" / "ledger.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported <= {"__future__", "math", "dataclasses", "datetime", "decimal", "types", "typing",
                        "execution.risk"}


def test_runs_sealed_and_does_not_change_inputs(monkeypatch):
    a = acct(1_000, A=10)
    closes, targets = {"A": 20.0, "B": 5.0}, {"A": -0.3, "B": 0.5}
    expected = L.plan_orders(a, targets, closes)
    sealed(monkeypatch)
    got = L.plan_orders(a, targets, closes)
    a2, _ = L.fill(a, got, closes)
    a3, _ = L.mark(L.accrue(a2, MON)[0], closes, MON)
    monkeypatch.undo()
    assert got == expected
    assert a.positions == {"A": Position(10, 0.0)} and a.cash == 1_000
    assert closes == {"A": 20.0, "B": 5.0} and targets == {"A": -0.3, "B": 0.5}
