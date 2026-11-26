"""Splits in the ledger (§7 rule 11, v1.18): the pure math (execution/ledger.py)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from execution import ledger as L  # noqa: E402
from execution.ledger import Account, Order, Position  # noqa: E402


def acct(qty, avg=100.0, cash=50_000.0):
    return Account(cash=cash, positions={"NVDA": Position(qty, avg)})


@pytest.mark.parametrize("qty,ratio,after", [(10.0, 4.0, 40.0), (-12.0, 4.0, -48.0), (15.0, 0.1, 1.5),
                                             (-15.0, 0.1, -1.5), (2.123456789, 0.1, 0.2123456789)])
def test_apply_split_is_exact_and_cash_free(qty, ratio, after):
    a, before, new = L.apply_split(acct(qty), "NVDA", ratio)
    assert before == Position(qty, 100.0)
    assert new.qty == pytest.approx(after, abs=1e-15) and new.avg_cost == pytest.approx(100.0 / ratio)
    assert a.cash == 50_000.0
    assert new.qty * new.avg_cost == pytest.approx(qty * 100.0)          # cost basis unchanged
    closes_pre, closes_post = {"NVDA": 120.0}, {"NVDA": 120.0 / ratio}
    assert a.value(closes_post) == pytest.approx(acct(qty).value(closes_pre))   # equity unchanged


def test_apply_split_not_held_and_bad_ratio():
    a = acct(10.0)
    assert L.apply_split(a, "AAPL", 4.0) == (a, None, None)
    for bad in (0, -2, float("nan")):
        with pytest.raises(L.LedgerError):
            L.apply_split(a, "NVDA", bad)


def test_restate_long_buy_forward():
    r = L.restate_orders(10.0, [Order("NVDA", "buy", 2.5)], 4.0)
    assert r[0]["qty_after"] == 10.0 and r[0]["target_after"] == 50.0 and r[0]["rounding"] == 0


def test_restate_long_close_reverse_is_exact():
    r = L.restate_orders(2.123456789, [Order("NVDA", "sell", 2.123456789)], 0.1)
    assert r[0]["qty_after"] == pytest.approx(0.2123456789)               # closes the whole holding, no dust


def test_restate_short_open_reverse_rounds_towards_zero():
    # flat, sell 15 to open a short: target -15 x 0.1 = -1.5 -> -1 whole share, 0.5 logged
    r = L.restate_orders(0.0, [Order("NVDA", "sell", 15.0)], 0.1)
    assert r[0]["qty_after"] == 1.0 and r[0]["target_after"] == -1.0
    assert r[0]["rounding"] == pytest.approx(-0.5)


def test_restate_short_cover_of_a_fractional_short():
    # short 15, buy 15 to cover: post-split short -1.5 is covered exactly
    r = L.restate_orders(-15.0, [Order("NVDA", "buy", 15.0)], 0.1)
    assert r[0]["qty_after"] == pytest.approx(1.5)


def test_restate_short_order_that_would_reverse_is_cancelled():
    # short 15, sell 1 more: target -16 -> -1.6 -> -1, holding -1.5: a buy of 0.5, so cancelled
    r = L.restate_orders(-15.0, [Order("NVDA", "sell", 1.0)], 0.1)
    assert r[0]["qty_after"] is None and "other side" in r[0]["cancelled"]


def test_restate_to_nothing_is_cancelled():
    r = L.restate_orders(0.0, [Order("NVDA", "sell", 5.0)], 0.1)          # -0.5 -> 0
    assert r[0]["qty_after"] is None and r[0]["cancelled"] == "restated to nothing"


def test_restate_flip_long_to_short():
    orders = [Order("NVDA", "sell", 10.0), Order("NVDA", "sell", 5.0)]       # 10 long -> 5 short
    fwd = L.restate_orders(10.0, orders, 4.0)
    assert [x["qty_after"] for x in fwd] == [40.0, 20.0]
    rev = L.restate_orders(10.0, orders, 0.1)                                 # 1 long -> -0.5 -> 0
    assert rev[0]["qty_after"] == pytest.approx(1.0) and rev[1]["qty_after"] is None


def test_target_shares_unchanged_by_the_refactor():
    assert L.target_shares(0.1, 100_000, 300.0) == 33.333333333
    assert L.target_shares(-0.1, 100_000, 300.0) == -33.0
    assert L.target_shares(-0.03, 100_000, 300.0) == -10.0                   # snapped, not 9
