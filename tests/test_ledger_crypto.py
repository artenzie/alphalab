"""Crypto fills in the ledger (protocol §7 rule 4 / 11, §13, v1.12): settle_day for C0 and C1.

Daily bars as the ingest stores them (test_snapshot.add: open = 0.995 x close,
fetched at 00:05Z); minute fill bars seeded by hand into crypto_fill_bars.
Decision D (a calendar day) is placed at 00:15Z on D+1 and settled on D+1.
"""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from data import calendar as cal  # noqa: E402
from data import db  # noqa: E402
from execution import ledger as L  # noqa: E402
from execution import ledger_db as LD  # noqa: E402
from test_snapshot import add  # noqa: E402

UTC = timezone.utc
MON, TUE, WED = date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18)
FRI, SAT, SUN = date(2026, 11, 20), date(2026, 11, 21), date(2026, 11, 22)
BTC, ETH = "BTC/USD", "ETH/USD"
BPS, FEE = L.SLIPPAGE_BPS / 1e4, L.CRYPTO_FEE


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "t.db")
    yield c
    c.close()


def decide_at(d):
    return cal.crypto_times(d).decide_at


def settled_by(d, minutes=20):
    """A time after d's daily bar has closed (00:00Z on d+1) and the fill bars are stored."""
    return cal.crypto_times(d).close + timedelta(minutes=minutes)


def fill_bar(conn, sym, decision_date, minute, open_, fetched_at=None):
    w0 = decide_at(decision_date)
    ts = w0 + timedelta(minutes=minute)
    with conn:
        return conn.execute(
            """INSERT INTO crypto_fill_bars (symbol, decision_date, window_start, bar_ts, open, high, low, close,
                   volume, feed, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1.0, 'crypto', ?)""",
            (sym, decision_date.isoformat(), db.utc_ts(w0), db.utc_ts(ts), open_, open_ * 1.001, open_ * 0.999,
             open_, db.utc_ts(fetched_at or (w0 + timedelta(hours=1, minutes=5))))).lastrowid


def decision(conn, agent, d, decided_at=None):
    t = cal.crypto_times(d)
    row = conn.execute("SELECT snapshot_id FROM snapshots WHERE snapshot_date = ? AND asset_class = 'crypto'",
                       (d.isoformat(),)).fetchone()
    with conn:
        sid = row[0] if row else conn.execute(
            "INSERT INTO snapshots (asset_class, snapshot_date, cutoff_ts, built_at, payload, sha256, checks, "
            "checks_passed) VALUES ('crypto', ?, ?, ?, '{}', ?, '[]', 1)",
            (d.isoformat(), db.utc_ts(t.snapshot_at), db.utc_ts(t.snapshot_at), "0" * 64)).lastrowid
        return conn.execute(
            "INSERT INTO decisions (agent_id, snapshot_id, decided_at, status, parsed_targets) "
            "VALUES (?, ?, ?, 'ok', '{}')", (agent, sid, db.utc_ts(decided_at or t.decide_at))).lastrowid


def settle(conn, agent, d, now=None):
    return LD.settle_day(conn, agent, d, now or settled_by(d))


def place(conn, agent, d, closes, targets, created_at=None):
    created_at = created_at or decide_at(d)
    did = decision(conn, agent, d, created_at) if targets is not None else None
    return LD.place_orders(conn, agent, d, closes, targets, created_at, did)


def fills(conn):
    return conn.execute("SELECT * FROM fills ORDER BY fill_id").fetchall()


def c0_buys_on_monday(conn, agent="C0", weight=1.0, created_at=None):
    add(conn, BTC, MON, 65000.0)
    settle(conn, agent, MON)
    return place(conn, agent, MON, {BTC: 65000.0}, {BTC: weight}, created_at)


# ---------------------------------------------------------------------------

def test_first_day_marks_starting_cash_and_every_calendar_day_settles(conn):
    for d in (SAT, SUN):
        res = settle(conn, "C1", d)
        assert res.status == "settled" and res.mark.equity == L.START_CASH


def test_fill_at_the_first_minute_bar_open_with_fee_and_slippage(conn):
    """C1 buys 40% BTC (no cash-floor scaling): exact §7 rule 4 arithmetic."""
    placed = c0_buys_on_monday(conn, agent="C1", weight=0.4)
    qty = L.round_long(0.4 * 100_000 / 65000.0)
    assert [(o.side, o.qty) for o in placed.orders] == [("buy", qty)]
    bid = fill_bar(conn, BTC, MON, 0, 65100.0)
    fill_bar(conn, BTC, MON, 1, 70000.0)
    add(conn, BTC, TUE, 66000.0)
    res = settle(conn, "C1", TUE)
    f = res.fills[0]
    price = 65100.0 * (1 + BPS)
    assert (f.reference_price, f.fill_price, f.qty) == (65100.0, pytest.approx(price), qty)
    assert f.fee_usd == pytest.approx(FEE * qty * price)
    assert res.mark.cash == pytest.approx(100_000 - qty * price * (1 + FEE))
    assert res.mark.equity == pytest.approx(res.mark.cash + qty * 66000.0)
    row = fills(conn)[0]
    assert row["filled_at"] == "2026-11-17T00:15:00.000Z" and row["reference_price"] == 65100.0
    assert json.loads(row["detail"])["fill_bar_id"] == bid
    eq = conn.execute("SELECT * FROM equity WHERE as_of_date = ?", (TUE.isoformat(),)).fetchone()
    assert eq["trading_fees_usd"] == pytest.approx(f.fee_usd)
    pos = conn.execute("SELECT * FROM positions").fetchone()
    assert (pos["asset_class"], pos["symbol"], pos["mark_price"]) == ("crypto", BTC, 66000.0)


def test_c0_full_buy_is_scaled_by_the_cash_floor_including_the_fee(conn):
    c0_buys_on_monday(conn)
    fill_bar(conn, BTC, MON, 0, 65000.0)                 # even at an unchanged price the fee overdraws
    add(conn, BTC, TUE, 65000.0)
    res = settle(conn, "C0", TUE)
    assert len(res.floor_events) == 1 and 0 <= res.mark.cash < 65000.0 * 1e-9 * 1.01   # one 9-dp share step
    q = 100_000 / 65000.0
    k = 100_000 / (L.round_long(q) * 65000.0 * (1 + BPS) * (1 + FEE))
    assert res.fills[0].qty == pytest.approx(L.round_long(q) * k, abs=1e-8)
    ev = conn.execute("SELECT * FROM risk_events").fetchone()
    assert (ev["rule"], ev["symbol"]) == ("cash_floor", BTC)


def test_order_written_after_0015_fills_at_the_next_whole_minute(conn):
    """An AI call returning at 00:16:40 can't fill at the 00:15 or 00:16 bar."""
    c0_buys_on_monday(conn, agent="C1", weight=0.4, created_at=decide_at(MON) + timedelta(minutes=1, seconds=40))
    fill_bar(conn, BTC, MON, 0, 65000.0)
    fill_bar(conn, BTC, MON, 1, 65050.0)
    fill_bar(conn, BTC, MON, 2, 65200.0)
    add(conn, BTC, TUE, 66000.0)
    res = settle(conn, "C1", TUE)
    assert res.fills[0].reference_price == 65200.0
    assert fills(conn)[0]["filled_at"] == "2026-11-17T00:17:00.000Z"


def test_order_on_a_whole_minute_uses_that_minute():
    t = datetime(2026, 11, 17, 0, 16, tzinfo=UTC)
    assert LD.ceil_minute(t) == t
    assert LD.ceil_minute(t + timedelta(milliseconds=1)) == t + timedelta(minutes=1)


def test_no_fill_without_a_minute_bar(conn):
    c0_buys_on_monday(conn, agent="C1", weight=0.4)
    add(conn, BTC, TUE, 66000.0)
    with pytest.raises(LD.PriceNotAvailable, match="minute bar"):
        settle(conn, "C1", TUE)
    assert LD.pending_orders(conn, "C1") and not fills(conn)


def test_no_fill_from_a_minute_bar_fetched_after_now(conn):
    c0_buys_on_monday(conn, agent="C1", weight=0.4)
    add(conn, BTC, TUE, 66000.0)
    fill_bar(conn, BTC, MON, 0, 65100.0, fetched_at=settled_by(TUE, minutes=30))
    with pytest.raises(LD.PriceNotAvailable):
        settle(conn, "C1", TUE, now=settled_by(TUE, minutes=20))
    assert settle(conn, "C1", TUE, now=settled_by(TUE, minutes=31)).fills[0].reference_price == 65100.0


def test_no_fill_bar_used_from_before_the_decision_or_after_the_window(conn):
    c0_buys_on_monday(conn, agent="C1", weight=0.4, created_at=decide_at(MON) + timedelta(minutes=61))
    fill_bar(conn, BTC, MON, 0, 65100.0)
    fill_bar(conn, BTC, MON, 59, 65100.0)
    add(conn, BTC, TUE, 66000.0)
    with pytest.raises(LD.PriceNotAvailable):
        settle(conn, "C1", TUE)


def test_latest_version_of_a_fill_bar_known_at_now_is_used(conn):
    c0_buys_on_monday(conn, agent="C1", weight=0.4)
    fill_bar(conn, BTC, MON, 0, 65100.0, fetched_at=decide_at(MON) + timedelta(minutes=5))
    fill_bar(conn, BTC, MON, 0, 65120.0, fetched_at=decide_at(MON) + timedelta(minutes=50))
    add(conn, BTC, TUE, 66000.0)
    assert settle(conn, "C1", TUE).fills[0].reference_price == 65120.0


def test_no_mark_before_the_daily_bar_closes(conn):
    c0_buys_on_monday(conn, agent="C1", weight=0.4)
    fill_bar(conn, BTC, MON, 0, 65100.0)
    with pytest.raises(LD.PriceNotAvailable, match="closed, stored bar"):
        settle(conn, "C1", TUE, now=decide_at(MON) + timedelta(hours=2))   # TUE's bar not stored or closed


def test_sell_receives_open_less_slippage_and_fee(conn):
    c0_buys_on_monday(conn, agent="C1", weight=0.4)
    fill_bar(conn, BTC, MON, 0, 65100.0)
    add(conn, BTC, TUE, 66000.0)
    settle(conn, "C1", TUE)
    acct = LD.load_account(conn, "C1", as_of=TUE)
    held = acct.shares(BTC)
    place(conn, "C1", TUE, {BTC: 66000.0}, {})
    fill_bar(conn, BTC, TUE, 0, 66500.0)
    add(conn, BTC, WED, 67000.0)
    res = settle(conn, "C1", WED)
    f = res.fills[0]
    price = 66500.0 * (1 - BPS)
    assert (f.side, f.qty) == ("sell", held)
    assert res.mark.cash == pytest.approx(acct.cash + held * price * (1 - FEE))
    assert res.mark.equity == res.mark.cash and not LD.load_account(conn, "C1", as_of=WED).positions


def test_restart_safety_and_next_decision_waits(conn):
    c0_buys_on_monday(conn)
    fill_bar(conn, BTC, MON, 0, 65000.0)
    add(conn, BTC, TUE, 66000.0)
    settle(conn, "C0", TUE)
    assert settle(conn, "C0", TUE).status == "already_settled"
    n = len(fills(conn))
    assert place(conn, "C0", TUE, {BTC: 66000.0}, None).orders == ()   # C0 holds
    add(conn, BTC, WED, 67000.0)
    assert settle(conn, "C0", WED).fills == () and len(fills(conn)) == n


def test_crypto_has_no_delisting(conn):
    with pytest.raises(L.LedgerError, match="delisting"):
        LD.settle_day(conn, "C0", MON, settled_by(MON), delisted={BTC: 1.0})


def test_snapshot_never_sees_the_fill_bars(conn):
    """Rule 1: the crypto snapshot is the same with or without stored minute fill bars."""
    from data.snapshot import build_snapshot
    for i in range(30):
        add(conn, BTC, MON - timedelta(days=29 - i), 60000.0 + 100 * i)
    now = settled_by(TUE, minutes=120)
    before = build_snapshot(conn, "crypto", MON, "live", now=now, symbols=[BTC])
    fill_bar(conn, BTC, MON, 0, 99999.0, fetched_at=decide_at(MON) + timedelta(minutes=1))
    fill_bar(conn, BTC, SUN, 0, 88888.0, fetched_at=decide_at(SUN) + timedelta(minutes=1))
    after = build_snapshot(conn, "crypto", MON, "live", now=now, symbols=[BTC])
    assert after.sha256 == before.sha256 and after.history_sha256 == before.history_sha256
    assert before.payload["symbols"][BTC]["last_close"] == 62900.0


# ---------------------------------------------------------------------------
# v1.13: the fill window follows the actual decision time (late runs)
# ---------------------------------------------------------------------------

GOLDEN = ROOT / "tests" / "fixtures" / "crypto_on_time_settle_v1_12.json"


def minute_bar(conn, sym, decision_date, ts, open_, window_start, fetched_at=None):
    """A minute bar stored by a fetch of the window starting at window_start."""
    with conn:
        return conn.execute(
            """INSERT INTO crypto_fill_bars (symbol, decision_date, window_start, bar_ts, open, high, low, close,
                   volume, feed, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1.0, 'crypto', ?)""",
            (sym, decision_date.isoformat(), db.utc_ts(window_start), db.utc_ts(ts), open_, open_ * 1.001,
             open_ * 0.999, open_, db.utc_ts(fetched_at or window_start + timedelta(hours=1, minutes=5)))).lastrowid


def window_fetched(conn, decision_date, start, fetched_at=None, symbols=(BTC, ETH)):
    """fill_prices' record that the whole window [start, start + 60 min) was fetched after it closed."""
    with conn:
        conn.execute("INSERT INTO crypto_fill_windows (decision_date, window_start, window_end, symbols, fetched_at) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (decision_date.isoformat(), db.utc_ts(start), db.utc_ts(start + timedelta(minutes=60)),
                      json.dumps(list(symbols)), db.utc_ts(fetched_at or start + timedelta(minutes=65))))


def on_time_scenario(conn):
    """C0 and C1 decide on MON, both orders written at one shared record time 00:15:41Z (on time)."""
    for aid in ("C0", "C1"):
        if aid == "C0":
            add(conn, BTC, MON, 65000.0)
        settle(conn, aid, MON)
    t = decide_at(MON) + timedelta(seconds=41)
    place(conn, "C0", MON, {BTC: 65000.0}, {BTC: 1.0}, t)
    place(conn, "C1", MON, {BTC: 65000.0, ETH: 3000.0}, {BTC: 0.3, ETH: 0.2}, t)
    for m, p in ((0, 65010.0), (1, 65100.0), (2, 65200.0)):
        fill_bar(conn, BTC, MON, m, p)
    for m, p in ((1, 3001.0), (2, 3002.0)):
        fill_bar(conn, ETH, MON, m, p)
    add(conn, BTC, TUE, 66000.0)
    add(conn, ETH, TUE, 3100.0)
    for aid in ("C0", "C1"):
        settle(conn, aid, TUE)
    return {t: [{k: r[k] for k in r.keys() if k != "recorded_at"} for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]
            for t in ("orders", "fills", "positions", "equity", "risk_events")}


def test_on_time_run_settles_exactly_as_before_v1_13(conn):
    """Regression: every ledger row of an on-time day equals the output of the v1.12 ledger (fixture
    captured from it before the change)."""
    assert on_time_scenario(conn) == json.loads(GOLDEN.read_text())


def test_late_decision_fills_at_the_first_bar_at_or_after_its_record_time(conn):
    """MON's decision recorded at 03:09:20Z TUE (late): fills at the 03:10 bar of the late window,
    never at the on-time window's 00:15 bar or at the 03:09 bar before the decision."""
    rec = decide_at(MON) + timedelta(hours=2, minutes=54, seconds=20)          # 03:09:20Z
    t_ref = LD.ceil_minute(rec)                                                # 03:10Z
    c0_buys_on_monday(conn, agent="C1", weight=0.4, created_at=rec)
    fill_bar(conn, BTC, MON, 0, 60000.0)                                       # on-time window: not used
    minute_bar(conn, BTC, MON, t_ref - timedelta(minutes=1), 61000.0, t_ref - timedelta(minutes=1))
    bid = minute_bar(conn, BTC, MON, t_ref, 65300.0, t_ref)
    minute_bar(conn, BTC, MON, t_ref + timedelta(minutes=1), 69000.0, t_ref)
    add(conn, BTC, TUE, 66000.0)
    res = settle(conn, "C1", TUE)
    assert res.fills[0].reference_price == 65300.0
    row = fills(conn)[0]
    assert row["filled_at"] == "2026-11-17T03:10:00.000Z" and json.loads(row["detail"])["fill_bar_id"] == bid
    assert row["filled_at"] >= conn.execute("SELECT created_at FROM orders").fetchone()[0]


def test_late_window_runs_to_t_ref_plus_60_minutes(conn):
    """The first bar of a late window may be 50 minutes in: past 01:15Z, still inside [t_ref, t_ref + 60)."""
    rec = decide_at(MON) + timedelta(hours=3)                                  # 03:15:00Z, a whole minute
    c0_buys_on_monday(conn, agent="C1", weight=0.4, created_at=rec)
    minute_bar(conn, BTC, MON, rec + timedelta(minutes=50), 65400.0, rec)
    add(conn, BTC, TUE, 66000.0)
    assert settle(conn, "C1", TUE).fills[0].reference_price == 65400.0


def test_no_bar_in_a_fetched_window_cancels_that_order_and_logs_it(conn):
    """BTC has no minute bar in [t_ref, t_ref + 60) after the whole window was fetched: its order is
    cancelled (reason no_fill_bar) while ETH fills; the day still marks."""
    rec = decide_at(MON) + timedelta(hours=2, minutes=54, seconds=20)
    t_ref = LD.ceil_minute(rec)
    add(conn, BTC, MON, 65000.0)
    add(conn, ETH, MON, 3000.0)
    settle(conn, "C1", MON)
    place(conn, "C1", MON, {BTC: 65000.0, ETH: 3000.0}, {BTC: 0.3, ETH: 0.2}, rec)
    minute_bar(conn, BTC, MON, t_ref + timedelta(minutes=60), 65000.0, t_ref + timedelta(minutes=60))  # just after
    minute_bar(conn, ETH, MON, t_ref + timedelta(minutes=3), 3010.0, t_ref)
    window_fetched(conn, MON, t_ref)
    add(conn, BTC, TUE, 66000.0)
    add(conn, ETH, TUE, 3100.0)
    res = settle(conn, "C1", TUE)
    assert [f.symbol for f in res.fills] == [ETH] and res.cancelled == (BTC,)
    rows = conn.execute("SELECT f.*, o.symbol FROM fills f JOIN orders o USING (order_id) ORDER BY fill_id").fetchall()
    btc = next(r for r in rows if r["symbol"] == BTC)
    assert btc["status"] == "cancelled" and btc["filled_at"] == db.utc_ts(t_ref)
    detail = json.loads(btc["detail"])
    assert detail["reason"] == "no_fill_bar" and detail["window_start"] == db.utc_ts(t_ref)
    assert not LD.pending_orders(conn, "C1")
    acct = LD.load_account(conn, "C1", as_of=TUE)
    assert BTC not in acct.positions and acct.shares(ETH) > 0


def test_no_bar_before_the_window_is_fully_fetched_waits_instead_of_cancelling(conn):
    """Without fill_prices' record of the closed window (or before it was written), a missing bar may
    still arrive: PriceNotAvailable, nothing written."""
    rec = decide_at(MON) + timedelta(hours=3)
    c0_buys_on_monday(conn, agent="C1", weight=0.4, created_at=rec)
    add(conn, BTC, TUE, 66000.0)
    with pytest.raises(LD.PriceNotAvailable, match="minute bar"):
        settle(conn, "C1", TUE)
    window_fetched(conn, MON, rec, fetched_at=settled_by(TUE, minutes=30))
    with pytest.raises(LD.PriceNotAvailable):
        settle(conn, "C1", TUE, now=settled_by(TUE, minutes=29))            # the record isn't known yet
    assert not fills(conn)
    assert settle(conn, "C1", TUE, now=settled_by(TUE, minutes=31)).cancelled == (BTC,)


def test_a_window_record_for_another_start_does_not_cancel(conn):
    """The on-time window [00:15, 01:15) was fetched; a decision recorded at 03:15Z needs its own window."""
    rec = decide_at(MON) + timedelta(hours=3)
    c0_buys_on_monday(conn, agent="C1", weight=0.4, created_at=rec)
    window_fetched(conn, MON, decide_at(MON))
    add(conn, BTC, TUE, 66000.0)
    with pytest.raises(LD.PriceNotAvailable):
        settle(conn, "C1", TUE)


def test_decision_after_the_next_days_snapshot_cutoff_is_missed(conn):
    """Max lateness: MON's orders written after TUE's snapshot cutoff (00:10Z WED) are not placed."""
    add(conn, BTC, MON, 65000.0)
    settle(conn, "C1", MON)
    cutoff = cal.crypto_times(TUE).snapshot_at
    placed = place(conn, "C1", MON, {BTC: 65000.0}, {BTC: 0.4}, created_at=cutoff + timedelta(milliseconds=1))
    assert placed.status == "missed" and placed.orders == () and not LD.pending_orders(conn, "C1")
    ok = place(conn, "C0", MON, {BTC: 65000.0}, {BTC: 1.0}, created_at=cutoff - timedelta(hours=1)) \
        if settle(conn, "C0", MON) else None
    assert ok.status == "placed"
