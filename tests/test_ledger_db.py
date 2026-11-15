"""Ledger database layer (protocol §7 v1.5 rules 8-11, §9): execution/ledger_db.py on a temp database.

No network: the calendar cache and bars are seeded by hand (helpers from
test_snapshot). Bars are stored as the ingest stores them, fetched at close + 1:30,
with open = 0.995 x close.
"""

import json
import sqlite3
import sys
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from data import calendar as cal  # noqa: E402
from data import db  # noqa: E402
from execution import ledger as L  # noqa: E402
from execution import ledger_db as LD  # noqa: E402
from test_snapshot import add, close_of, seed_calendar  # noqa: E402

UTC = timezone.utc
FRI, MON, TUE, WED = date(2026, 11, 13), date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18)
SAT = date(2026, 11, 14)


def after(d, minutes=90):
    """The protocol's fetch time for d (bars stored), or later."""
    return close_of(d) + timedelta(minutes=minutes)


def decide_at(d):
    return close_of(d) + cal.DECIDE_AFTER_CLOSE


def no_network():
    raise RuntimeError("no network in tests")


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "t.db")
    seed_calendar(c)
    yield c
    c.close()


def decision(conn, agent_id, d):
    """A stored snapshot + decision row for d (FKs for decision orders)."""
    cutoff = db.utc_ts(close_of(d) + cal.SNAPSHOT_AFTER_CLOSE)
    row = conn.execute("SELECT snapshot_id FROM snapshots WHERE snapshot_date = ?", (d.isoformat(),)).fetchone()
    with conn:
        sid = row[0] if row else conn.execute(
            "INSERT INTO snapshots (asset_class, snapshot_date, cutoff_ts, built_at, payload, sha256, checks, "
            "checks_passed) VALUES ('us_equity', ?, ?, ?, '{}', ?, '[]', 1)",
            (d.isoformat(), cutoff, cutoff, "0" * 64)).lastrowid
        return conn.execute(
            "INSERT INTO decisions (agent_id, snapshot_id, decided_at, status, parsed_targets) "
            "VALUES (?, ?, ?, 'ok', '{}')", (agent_id, sid, db.utc_ts(decide_at(d)))).lastrowid


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def settle(conn, agent, d, now=None, **kw):
    return LD.settle_day(conn, agent, d, now or after(d), trading=no_network, **kw)


def place(conn, agent, d, closes, targets):
    did = decision(conn, agent, d) if targets is not None else None
    return LD.place_orders(conn, agent, d, closes, targets, decide_at(d), did)


def buy_a_on_monday(conn, agent="4", weight=0.5):
    add(conn, "A", MON, 100.0)
    settle(conn, agent, MON)
    return place(conn, agent, MON, {"A": 100.0}, {"A": weight})


# ---------------------------------------------------------------------------
# A day's cycle
# ---------------------------------------------------------------------------

def test_first_day_marks_starting_cash(conn):
    res = settle(conn, "4", MON)
    assert res.status == "settled" and res.mark.equity == L.START_CASH
    row = conn.execute("SELECT * FROM equity").fetchone()
    assert (row["agent_id"], row["as_of_date"], row["cash"], row["equity"]) == ("4", "2026-11-16", 1e5, 1e5)


def test_buy_fills_at_next_open_and_marks_at_close(conn):
    placed = buy_a_on_monday(conn)
    assert placed.status == "placed" and [(o.side, o.qty) for o in placed.orders] == [("buy", 500.0)]
    o = conn.execute("SELECT * FROM orders").fetchone()
    assert (o["origin"], o["venue"], o["side"], o["qty"], o["target_weight"]) == ("decision", "ledger", "buy", 500, 0.5)
    assert LD.pending_orders(conn, "4")

    add(conn, "A", TUE, 110.0)                                 # open 109.45
    res = settle(conn, "4", TUE)
    fill_price = 109.45 * 1.0005                               # 109.504725
    assert res.fills[0].fill_price == pytest.approx(fill_price)
    f = conn.execute("SELECT * FROM fills").fetchone()
    assert f["status"] == "filled" and f["reference_price"] == pytest.approx(109.45)
    assert f["filled_at"] == db.utc_ts(cal.et_to_utc(TUE, dtime(9, 30)))
    assert json.loads(f["detail"])["slippage_usd"] == pytest.approx(500 * 109.45 * 0.0005)
    eq = conn.execute("SELECT * FROM equity WHERE as_of_date = '2026-11-17'").fetchone()
    assert eq["cash"] == pytest.approx(100_000 - 500 * fill_price)
    assert eq["equity"] == pytest.approx(100_000 - 500 * fill_price + 500 * 110)
    p = conn.execute("SELECT * FROM positions").fetchone()
    assert (p["symbol"], p["qty"], p["mark_price"], p["market_value"]) == ("A", 500, 110, 55_000)
    assert p["unrealised_pnl"] == pytest.approx((110 - fill_price) * 500)
    assert not LD.pending_orders(conn, "4")
    acct = LD.load_account(conn, "4", as_of=TUE)
    assert acct.shares("A") == 500 and acct.eod_date == TUE


def test_restart_idempotency(conn):
    buy_a_on_monday(conn)
    did = conn.execute("SELECT decision_id FROM decisions").fetchone()[0]
    again = LD.place_orders(conn, "4", MON, {"A": 100.0}, {"A": 0.5}, decide_at(MON), did)
    assert again.status == "already_placed" and count(conn, "orders") == 1
    add(conn, "A", TUE, 110.0)
    settle(conn, "4", TUE)
    counts = {t: count(conn, t) for t in ("orders", "fills", "positions", "equity")}
    assert settle(conn, "4", TUE).status == "already_settled"
    assert settle(conn, "4", TUE, now=after(TUE, 600)).status == "already_settled"
    assert {t: count(conn, t) for t in counts} == counts


def test_fills_on_the_day_alone_block_a_rerun(conn):
    """Rule 10 is keyed on fills too, not only on the equity row."""
    buy_a_on_monday(conn)
    oid = conn.execute("SELECT order_id FROM orders").fetchone()[0]
    with conn:
        conn.execute("INSERT INTO fills (order_id, status, filled_at, qty, reference_price, fill_price) "
                     "VALUES (?, 'filled', ?, 500, 100, 100)", (oid, db.utc_ts(cal.et_to_utc(TUE, dtime(9, 30)))))
    add(conn, "A", TUE, 110.0)
    assert settle(conn, "4", TUE).status == "already_settled"


def test_hold_places_nothing_and_weekend_accrual_through_the_db(conn):
    add(conn, "A", FRI, 50.0)
    settle(conn, "4", FRI)
    place(conn, "4", FRI, {"A": 50.0}, {"A": -0.1})             # -10000 / 50 = -200 shares
    add(conn, "A", MON, 50.0)
    settle(conn, "4", MON)                                     # filled Mon open; Fri held nothing
    assert place(conn, "4", MON, {"A": 50.0}, None).orders == ()
    add(conn, "A", TUE, 50.0)
    res = settle(conn, "4", TUE)
    assert res.borrow == pytest.approx(0.005 / 360 * 200 * 50)   # Mon only, on Mon's short value
    add(conn, "A", WED, 50.0)
    assert settle(conn, "4", WED).borrow == pytest.approx(0.005 / 360 * 10_000)


# ---------------------------------------------------------------------------
# Rule 11: no early prices
# ---------------------------------------------------------------------------

def test_no_fill_before_next_bar_is_stored(conn):
    buy_a_on_monday(conn)
    with pytest.raises(LD.PriceNotAvailable):
        settle(conn, "4", TUE)                                 # no Tuesday bar at all
    assert count(conn, "fills") == 0 and count(conn, "equity") == 1


def test_no_fill_from_a_bar_fetched_after_now(conn):
    buy_a_on_monday(conn)
    add(conn, "A", TUE, 110.0, fetched_at=after(TUE, 600))
    with pytest.raises(LD.PriceNotAvailable):
        settle(conn, "4", TUE, now=after(TUE, 120))
    assert settle(conn, "4", TUE, now=after(TUE, 601)).status == "settled"


def test_no_fill_from_an_unclosed_bar(conn):
    buy_a_on_monday(conn)
    midday = cal.et_to_utc(TUE, dtime(12, 0))
    add(conn, "A", TUE, 110.0, fetched_at=midday)              # a partial bar stored by mistake
    with pytest.raises(LD.PriceNotAvailable):
        settle(conn, "4", TUE, now=midday + timedelta(minutes=1))
    assert count(conn, "fills") == 0


def test_orders_created_after_the_open_wait_for_the_next_day(conn):
    add(conn, "A", MON, 100.0)
    settle(conn, "4", MON)
    did = decision(conn, "4", MON)
    LD.place_orders(conn, "4", MON, {"A": 100.0}, {"A": 0.5}, cal.et_to_utc(TUE, dtime(10, 0)), did)
    add(conn, "A", TUE, 110.0)
    assert settle(conn, "4", TUE).fills == ()


def test_not_a_trading_day_and_crypto_refused(conn):
    with pytest.raises(L.LedgerError):
        settle(conn, "4", SAT)
    with pytest.raises(LD.PriceNotAvailable):
        settle(conn, "C1", MON)


# ---------------------------------------------------------------------------
# Splits, delisting, margin
# ---------------------------------------------------------------------------

def test_split_between_decision_and_fill_stops_the_ledger(conn):
    buy_a_on_monday(conn)
    add(conn, "A", TUE, 110.0)
    settle(conn, "4", TUE)
    # 2:1 split effective Wed: Wednesday's fetch re-adjusts Tuesday (adj = raw / 2)
    add(conn, "A", TUE, 110.0, fetched_at=after(WED, -60), ratio=0.5)
    add(conn, "A", WED, 56.0)
    with pytest.raises(LD.SplitDetected):
        settle(conn, "4", WED)
    assert count(conn, "equity") == 2
    assert settle(conn, "4", WED, mode="backtest").status == "settled"   # adjusted prices: no guard


def test_backtest_mode_uses_adjusted_prices(conn):
    add(conn, "A", MON, 100.0)
    settle(conn, "4", MON, mode="backtest")
    place(conn, "4", MON, {"A": 100.0}, {"A": 0.5})
    add(conn, "A", TUE, 220.0, ratio=0.5)                      # adj open 109.45, adj close 110
    res = settle(conn, "4", TUE, mode="backtest")
    assert res.fills[0].reference_price == pytest.approx(109.45) and res.mark.values["A"] == pytest.approx(55_000)


def test_delisting_closes_at_last_price_and_cancels_pending(conn):
    add(conn, "A", MON, 100.0)
    add(conn, "B", MON, 10.0)
    settle(conn, "4", MON)
    place(conn, "4", MON, {"A": 100.0, "B": 10.0}, {"A": 0.5, "B": 0.1})
    add(conn, "A", TUE, 100.0)
    add(conn, "B", TUE, 10.0)
    settle(conn, "4", TUE)
    place(conn, "4", TUE, {"A": 100.0, "B": 10.0}, {"B": 0.2})   # B order pending; A untargeted
    add(conn, "A", WED, 100.0)                                 # B: acquired, no bar
    with pytest.raises(LD.PriceNotAvailable):
        settle(conn, "4", WED)
    res = settle(conn, "4", WED, delisted={"B": 10.0})
    assert res.cancelled == ("B",)
    assert [(f.symbol, f.side, f.origin) for f in res.fills] == [("B", "sell", "delisting")]
    assert res.fills[0].fill_price == pytest.approx(9.995)
    assert "B" not in LD.load_account(conn, "4", as_of=WED).positions
    statuses = dict(conn.execute("SELECT o.origin || ':' || f.status, COUNT(*) FROM fills f "
                                 "JOIN orders o USING (order_id) GROUP BY 1").fetchall())
    assert statuses == {"decision:filled": 2, "decision:cancelled": 1, "delisting:filled": 1}


def margin_breach(conn):
    """Agent 4 buys 2000 A (200%) on Tue's open at 99.5; A closes Wed at 60.

    Wed: cash 100000 - 2000 * 99.54975 = -99099.5, long 120000, equity 20900.5,
    gross 120000: 17% < 30%.
    """
    add(conn, "A", MON, 100.0)
    settle(conn, "4", MON)
    place(conn, "4", MON, {"A": 100.0}, {"A": 2.0})            # risk checks would clip this; the ledger doesn't
    add(conn, "A", TUE, 100.0)
    settle(conn, "4", TUE)
    add(conn, "A", WED, 60.0)
    res = settle(conn, "4", WED)
    cash = 100_000 - 2000 * 99.5 * 1.0005
    assert res.mark.equity == pytest.approx(cash + 120_000 + cash * 0.065 / 360)   # + 1 day interest
    assert res.mark.equity / res.mark.gross_exposure < 0.30
    return res


def test_margin_breach_on_a_hold_places_forced_reductions(conn):
    margin_breach(conn)
    placed = place(conn, "4", WED, {"A": 60.0}, None)
    assert [(o.side, o.origin) for o in placed.orders] == [("sell", "margin")]
    o = conn.execute("SELECT * FROM orders WHERE origin = 'margin'").fetchone()
    ev = conn.execute("SELECT * FROM risk_events WHERE risk_event_id = ?", (o["risk_event_id"],)).fetchone()
    assert (ev["event_type"], ev["rule"], ev["symbol"]) == ("forced_reduction", "margin", "A")
    assert not placed.margin_superseded


def test_margin_breach_with_a_decision_uses_the_decision_and_logs_the_breach(conn):
    margin_breach(conn)
    placed = place(conn, "4", WED, {"A": 60.0}, {"A": 0.1})
    assert placed.margin_superseded and all(o.origin == "decision" for o in placed.orders)
    ev = conn.execute("SELECT * FROM risk_events").fetchone()
    assert json.loads(ev["detail"])["superseded_by_decision"] is True


def test_place_orders_needs_a_settled_day(conn):
    with pytest.raises(L.LedgerError):
        place(conn, "4", MON, {"A": 100.0}, {"A": 0.5})
    assert count(conn, "orders") == 0


def test_ledger_tables_stay_append_only(conn):
    buy_a_on_monday(conn)
    add(conn, "A", TUE, 110.0)
    settle(conn, "4", TUE)
    for table in ("orders", "fills", "positions", "equity"):
        assert count(conn, table) > 0
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            with conn:
                conn.execute(f"DELETE FROM {table}")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            with conn:
                conn.execute(f"UPDATE {table} SET recorded_at = recorded_at" if table != "orders"
                             else "UPDATE orders SET qty = qty")


# ---------------------------------------------------------------------------
# Cash floor (rule 12)
# ---------------------------------------------------------------------------

def test_cash_floor_gap_up_scales_the_buy_and_logs_it(conn):
    """Agent 2 (floored) sized 1000 A at Mon close 100; Tue opens at 109.45."""
    placed = buy_a_on_monday(conn, agent="2", weight=1.0)
    assert [(o.side, o.qty) for o in placed.orders] == [("buy", 1000.0)]
    add(conn, "A", TUE, 110.0)
    res = settle(conn, "2", TUE)
    k = 100_000 / (1000 * 109.45 * 1.0005)
    assert res.fills[0].qty == pytest.approx(1000 * k, abs=1e-8)
    assert 0 <= res.mark.cash < 1e-6
    ev = conn.execute("SELECT * FROM risk_events").fetchone()
    did = conn.execute("SELECT decision_id FROM orders").fetchone()[0]
    assert (ev["event_type"], ev["rule"], ev["symbol"], ev["decision_id"]) == ("clip", "cash_floor", "A", did)
    f = conn.execute("SELECT * FROM fills").fetchone()
    detail = json.loads(f["detail"])
    assert f["qty"] < 1000 and detail["risk_event_id"] == ev["risk_event_id"] and detail["requested_qty"] == 1000
    assert conn.execute("SELECT qty FROM orders").fetchone()[0] == 1000          # order row as placed
    assert settle(conn, "2", TUE).status == "already_settled" and count(conn, "risk_events") == 1
    add(conn, "A", WED, 110.0)
    assert settle(conn, "2", WED).interest == 0


def test_cash_floor_cancels_an_unfunded_dust_buy(conn):
    """Fully invested (cash < $0.000001): a buy with no sells to fund it scales to dust and is cancelled."""
    buy_a_on_monday(conn, agent="2", weight=1.0)
    add(conn, "A", TUE, 110.0)
    add(conn, "B", TUE, 10.0)
    settle(conn, "2", TUE)                                  # fully invested, cash ~0
    place(conn, "2", TUE, {"A": 110.0, "B": 10.0}, {"B": 0.1})   # a buy with no sells to fund it
    add(conn, "A", WED, 110.0)
    add(conn, "B", WED, 10.0)
    res = settle(conn, "2", WED)
    assert len(res.floor_events) == 1 and res.fills == ()
    assert 0 <= res.mark.cash < 1e-6
    oid = conn.execute("SELECT order_id FROM orders WHERE symbol = 'B'").fetchone()[0]
    f = conn.execute("SELECT * FROM fills WHERE order_id = ?", (oid,)).fetchone()
    ev = conn.execute("SELECT * FROM risk_events WHERE symbol = 'B' AND rule = 'cash_floor'").fetchone()
    assert f["status"] == "cancelled"
    assert json.loads(f["detail"]) == {"reason": "cash_floor", "risk_event_id": ev["risk_event_id"]}
    d = json.loads(ev["detail"])
    assert d["cancelled"] == "scaled value under $1" and d["applied_qty"] == 0 and ev["applied_weight"] == 0
    assert not LD.pending_orders(conn, "2")
    assert count(conn, "positions") == 2                    # Tue A, Wed A: no B position


def test_agents_without_the_floor_still_borrow_and_pay_interest(conn):
    buy_a_on_monday(conn, agent="4", weight=1.0)            # agent 4 shorts: not floored
    add(conn, "A", TUE, 110.0)
    res = settle(conn, "4", TUE)
    assert res.fills[0].qty == 1000 and res.mark.cash == pytest.approx(100_000 - 1000 * 109.45 * 1.0005)
    assert count(conn, "risk_events") == 0
    add(conn, "A", WED, 110.0)
    assert settle(conn, "4", WED).interest == pytest.approx(-res.mark.cash * 0.065 / 360)
