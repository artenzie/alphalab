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


# ===========================================================================
# On the database: settle_day / place_orders with a reviewed split (v1.18)
# ===========================================================================

import json  # noqa: E402
from datetime import date, timedelta  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data import corporate_actions as ca  # noqa: E402
from data import db  # noqa: E402
from execution import ledger_db as LD  # noqa: E402
from test_ledger_db import MON, TUE, WED, after, conn, count, place, settle  # noqa: E402,F401
from test_snapshot import add  # noqa: E402

THU = date(2026, 11, 19)
MOVE = 1.03                                      # Wednesday's close vs Tuesday's, in adjusted terms


def announce(conn, ratio, ex=WED, sym="A", at=None, ca_type=None):
    old, new = (1.0, ratio) if ratio >= 1 else (round(1 / ratio), 1.0)
    ca_type = ca_type or ("forward_split" if ratio >= 1 else "reverse_split")
    with conn:
        conn.execute("""INSERT INTO corporate_actions (ca_key, ca_type, symbol, ex_date, old_rate, new_rate, raw,
                        fetched_at) VALUES (?, ?, ?, ?, ?, ?, '{}', ?)""",
                     (f"{ca_type}|{sym}|{ex}", ca_type, sym, ex.isoformat(), old, new, db.utc_ts(at or after(ex, -30))))


def readjust(conn, ratio, through=TUE, sym="A", at=None, closes=None):
    """Alpaca re-adjusts the bars before the ex-date (adj = raw / ratio), as a new version."""
    for d, c in (closes or {MON: 100.0, TUE: 100.0}).items():
        if d <= through:
            add(conn, sym, d, c, fetched_at=at or after(WED, -60), ratio=1 / ratio)


def book(conn, agent, w1, w2):
    """Mon: open a position at weight w1 (filled Tue); Tue: an order to w2, pending across Wed."""
    add(conn, "A", MON, 100.0)
    settle(conn, agent, MON)
    place(conn, agent, MON, {"A": 100.0}, {"A": w1})
    add(conn, "A", TUE, 100.0)
    settle(conn, agent, TUE)
    return place(conn, agent, TUE, {"A": 100.0}, {"A": w2})


def eq(conn, agent, d):
    return conn.execute("SELECT * FROM equity WHERE agent_id = ? AND as_of_date = ? ORDER BY equity_id DESC",
                        (agent, d.isoformat())).fetchone()


def pos(conn, agent, d, sym="A"):
    return conn.execute("SELECT * FROM positions WHERE agent_id = ? AND as_of_date = ? AND symbol = ? "
                        "ORDER BY position_id DESC", (agent, d.isoformat(), sym)).fetchone()


SCENARIOS = {                     # ratio, Monday's weight (the holding), Tuesday's weight (the pending order)
    "4:1 long, buy pending": (4.0, 0.3, 0.4),
    "4:1 short, cover pending": (4.0, -0.3, -0.2),
    "1:10 long, sell pending": (0.1, 0.3, 0.15),
    "1:10 short, sell pending (fractional short, rounding)": (0.1, -0.155, -0.205),
}


@pytest.mark.parametrize("name", SCENARIOS)
def test_split_on_ex_date_with_an_order_pending_across_it(conn, name):
    ratio, w1, w2 = SCENARIOS[name]
    agent = "4"                                           # no cash floor; may short
    placed = book(conn, agent, w1, w2)
    assert len(placed.orders) == 1
    order = placed.orders[0]
    h_tue = LD.load_account(conn, agent, as_of=TUE).shares("A")
    avg_tue = pos(conn, agent, TUE)["avg_cost"]
    # Wed, the ex-date: Alpaca re-adjusted, announced, and Wednesday's raw bar is post-split
    readjust(conn, ratio)
    announce(conn, ratio)
    c_wed = 100.0 / ratio * MOVE
    add(conn, "A", WED, c_wed)
    assert [r["event"] for r in ca.review(conn, WED, ["A"], after(WED, -5))] == ["split"]
    res = settle(conn, agent, WED)
    assert res.status == "settled" and len(res.splits) == 1

    sp = conn.execute("SELECT * FROM split_events WHERE agent_id = ?", (agent,)).fetchone()
    assert (sp["symbol"], sp["ex_date"], sp["applied_on"], sp["ratio"]) == ("A", "2026-11-18", "2026-11-18", ratio)
    assert sp["qty_before"] == h_tue and sp["qty_after"] == pytest.approx(h_tue * ratio)
    assert sp["avg_cost_after"] == pytest.approx(avg_tue / ratio)
    restated = json.loads(sp["detail"])["orders"][0]
    tgt_exact = (h_tue + order.signed_qty) * ratio
    assert restated["target_after_exact"] == pytest.approx(tgt_exact)
    assert restated["target_after"] == L._round_target(tgt_exact)
    assert restated["rounding"] == pytest.approx(tgt_exact - restated["target_after"])

    f = conn.execute("SELECT * FROM fills ORDER BY fill_id DESC").fetchone()
    detail = json.loads(f["detail"])
    assert f["status"] == "filled" and f["qty"] == pytest.approx(restated["qty_after"])
    assert (detail["split_event_id"], detail["requested_qty"], detail["split_ratio"]) == \
        (sp["split_event_id"], order.qty, ratio)
    q = f["qty"] if order.side == "buy" else -f["qty"]
    p = pos(conn, agent, WED)
    assert p["qty"] == pytest.approx(restated["target_after"])            # shorts end on a whole target
    if w2 < 0:
        assert p["qty"] == round(p["qty"])

    # Continuity: equity moves only by the market move (in post-split shares) and the costs
    e0, e1 = eq(conn, agent, TUE), eq(conn, agent, WED)
    h_post = h_tue * ratio
    move = h_post * (c_wed - 100.0 / ratio) + q * (c_wed - f["fill_price"])
    assert e1["equity"] - e0["equity"] == pytest.approx(move - e1["borrow_fee_usd"] - e1["margin_interest_usd"],
                                                        abs=1e-6)
    assert e1["cash"] == pytest.approx(e0["cash"] - q * f["fill_price"] - e1["borrow_fee_usd"]
                                       - e1["margin_interest_usd"], abs=1e-6)       # the split moved no cash
    # AI input: avg cost and unrealised P&L in post-split terms, consistent with the mark
    assert p["unrealised_pnl"] == pytest.approx((p["mark_price"] - p["avg_cost"]) * p["qty"])

    # restart safety: settling again changes nothing; Thursday applies nothing again
    assert settle(conn, agent, WED).status == "already_settled"
    add(conn, "A", THU, c_wed)
    ca.review(conn, THU, ["A"], after(THU, -5))
    res = settle(conn, agent, THU)
    assert res.splits == () and count(conn, "split_events") == 1


def test_restated_to_nothing_is_cancelled_and_logged(conn):
    placed = book(conn, "4", 0.0, -0.0004)                  # flat; short 0.4 share -> rounds to 0, no order
    assert placed.orders == ()
    placed = book_short_five(conn)
    readjust(conn, 0.1)
    announce(conn, 0.1)
    add(conn, "A", WED, 1000.0)
    ca.review(conn, WED, ["A"], after(WED, -5))
    settle(conn, "6", WED)
    f = conn.execute("SELECT f.* FROM fills AS f JOIN orders AS o USING (order_id) WHERE o.agent_id = '6'"
                     ).fetchone()
    assert f["status"] == "cancelled"
    d = json.loads(f["detail"])
    assert d["reason"] == "split_rounding" and d["restated"]["target_after_exact"] == pytest.approx(-0.5)
    assert LD.load_account(conn, "6", as_of=WED).positions == {}


def book_short_five(conn):
    """Agent 6, flat on Tue, places a 5-share short (-$500 of $100k) pending across Wed."""
    settle(conn, "6", MON)
    settle(conn, "6", TUE)
    return place(conn, "6", TUE, {"A": 100.0}, {"A": -0.005})


def test_freeze_holds_at_last_close_cancels_and_rejects(conn):
    agent = "4"
    book(conn, agent, 0.3, 0.4)
    h = LD.load_account(conn, agent, as_of=TUE).shares("A")
    add(conn, "B", TUE, 50.0)                               # another stock, unaffected
    announce(conn, 4.0, ca_type="spin_off")                 # ambiguous: freeze
    add(conn, "A", WED, 25.0)
    rows = ca.review(conn, WED, ["A"], after(WED, -5))
    assert [r["event"] for r in rows] == ["freeze"]
    res = settle(conn, agent, WED)
    assert res.frozen == ("A",) and res.splits == () and res.fills == ()
    p = pos(conn, agent, WED)
    assert (p["qty"], p["mark_price"]) == (h, 100.0)            # held at Tuesday's close
    assert eq(conn, agent, WED)["equity"] == pytest.approx(eq(conn, agent, TUE)["equity"]
                                                           - eq(conn, agent, WED)["borrow_fee_usd"]
                                                           - eq(conn, agent, WED)["margin_interest_usd"])
    f = conn.execute("SELECT * FROM fills ORDER BY fill_id DESC").fetchone()
    assert f["status"] == "cancelled" and json.loads(f["detail"])["reason"] == "symbol_frozen"
    # the decision on Wednesday: orders in A are rejected and logged, B trades; A valued at its frozen mark
    placed = place(conn, agent, WED, {"A": 25.0, "B": 50.0}, {"A": 0.1, "B": 0.2})
    assert [o.symbol for o in placed.orders] == ["B"]
    ev = conn.execute("SELECT * FROM risk_events WHERE rule = 'symbol_frozen'").fetchone()
    assert (ev["event_type"], ev["symbol"], ev["requested_weight"]) == ("reject", "A", 0.1)
    pv = LD.load_account(conn, agent, as_of=WED).value({"A": 100.0, "B": 50.0})
    assert placed.orders[0].qty == pytest.approx(L.target_shares(0.2, pv, 50.0))


def test_book_closes_puts_frozen_stocks_at_their_mark(conn):
    book(conn, "4", 0.3, 0.4)
    announce(conn, 4.0, ca_type="spin_off")
    add(conn, "A", WED, 25.0)
    ca.review(conn, WED, ["A"], after(WED, -5))
    assert LD.book_closes(conn, WED, {"A": 25.0, "B": 7.0}, after(WED)) == {"A": 100.0, "B": 7.0}


# ===========================================================================
# Rule 1: what snapshots (and the AI input) see across a split
# ===========================================================================

import math  # noqa: E402
from dataclasses import replace  # noqa: E402

from agents.harness import prompt_input  # noqa: E402
from data import calendar as cal  # noqa: E402
from data import checks  # noqa: E402
from data import snapshot as sn  # noqa: E402
from strategies.base import AgentMemory, Portfolio, build_context  # noqa: E402
from test_snapshot import close_of, seed_calendar, sessions  # noqa: E402

EX18 = WED                                                    # Wed 18 Nov 2026: AAPL 4:1
HIST = sessions(date(2026, 1, 2), EX18)


def hist_price(sym, i):
    return {"AAPL": 200.0, "SPY": 600.0}[sym] * (1 + 0.03 * math.sin(i / 7) + 0.001 * i)


def market(path, split):
    """AAPL and SPY to Wed 18 Nov. split: AAPL's raw bars from the ex-date on are / 4, and Alpaca's
    re-adjusted history (adj = raw / 4 before the ex-date) is stored as new versions at Wednesday's fetch."""
    c = db.init_db(path)
    seed_calendar(c)
    for i, d in enumerate(HIST):
        for sym in ("AAPL", "SPY"):
            k = 4 if split and sym == "AAPL" and d >= EX18 else 1       # post-split: price / 4, volume x 4
            add(c, sym, d, hist_price(sym, i) / k, volume=(1e6 + 1000 * (i % 13)) * k)
    if split:
        for i, d in enumerate(HIST):
            if d < EX18:
                add(c, "AAPL", d, hist_price("AAPL", i), volume=1e6 + 1000 * (i % 13), ratio=0.25,
                    fetched_at=close_of(EX18) + cal.BARS_FETCH_AFTER_CLOSE)
    return c


def snap(conn, d):
    return sn.build_snapshot(conn, "us_equity", d, "live", now=close_of(d) + timedelta(hours=2),
                             symbols=["AAPL", "SPY"])


def test_snapshot_before_the_split_is_known_uses_prices_as_known_then(tmp_path):
    plain, split = market(tmp_path / "plain.db", False), market(tmp_path / "split.db", True)
    # Tue 17 Nov (decision day before the ex-date): identical to a world without the split
    assert snap(split, TUE).payload == snap(plain, TUE).payload
    assert snap(split, TUE).history("AAPL", 252) == snap(plain, TUE).history("AAPL", 252)
    # Wed 18 Nov: the re-adjusted history throughout. Prices / 4, every return and ratio unchanged
    a, b = snap(split, EX18).payload["symbols"]["AAPL"], snap(plain, EX18).payload["symbols"]["AAPL"]
    for k in ("last_close", "ma_20", "ma_100"):
        assert a[k] == pytest.approx(b[k] / 4, rel=1e-9), k
    assert a["closes_20"] == pytest.approx([x / 4 for x in b["closes_20"]], rel=1e-9)
    for k in ("ret_1", "ret_5", "ret_21", "ret_63", "vol_20", "z_20", "volume_ratio_20"):
        assert a[k] == pytest.approx(b[k], rel=1e-9, abs=1e-12), k
    assert abs(a["ret_1"]) < 0.1                               # no fake -75% day
    _, problems = checks.analyse_splits("AAPL", "us_equity", checks.load_bars(
        split, ["AAPL"], date(2026, 1, 2), close_of(EX18) + timedelta(hours=2))["AAPL"], date(2026, 1, 2))
    assert problems == []                                      # one clean 4:1 step: the data checks pass


def test_ai_input_is_consistent_across_the_split(tmp_path):
    plain, split = market(tmp_path / "plain.db", False), market(tmp_path / "split.db", True)
    ctx = build_context(EX18, sessions(date(2026, 11, 16), EX18))
    out = {}
    for name, c, shares, avg in (("plain", plain, 10.0, 190.0), ("split", split, 40.0, 47.5)):
        s = snap(c, EX18)
        close = s.payload["symbols"]["AAPL"]["last_close"]
        pf = Portfolio(cash=50_000.0, positions={"AAPL": shares}, value=50_000.0 + shares * close)
        mem = AgentMemory(starting_value=1e5, avg_cost={"AAPL": avg}, end_date=date(2027, 1, 8), trading_days_left=30)
        out[name] = prompt_input("8", s, pf, replace(ctx, memory=mem))["portfolio"]
    p, q = out["plain"], out["split"]
    assert p["value"] == pytest.approx(q["value"])
    pa, qa = p["positions"]["AAPL"], q["positions"]["AAPL"]
    assert qa["shares"] == 4 * pa["shares"] and qa["avg_cost"] == pytest.approx(pa["avg_cost"] / 4)
    assert qa["weight"] == pytest.approx(pa["weight"]) and qa["unrealised_pnl"] == pytest.approx(pa["unrealised_pnl"])
