"""data/corporate_actions.py: fetch and versioned storage, the history re-fetch after a split, verdicts."""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from data import checks, db, ingest  # noqa: E402
from data import corporate_actions as ca  # noqa: E402

import test_ingest as TI  # noqa: E402

UTC = timezone.utc
DAYS = TI.weekdays(date(2026, 10, 1), date(2026, 11, 13))      # Thu 1 Oct .. Fri 13 Nov
EX = date(2026, 11, 11)                                          # Wed: AAPL 4:1 ex-date


def bar_ts(d):
    return datetime(d.year, d.month, d.day, 5, tzinfo=UTC)


def after_close(d, minutes=90):
    return datetime(d.year, d.month, d.day, 21, tzinfo=UTC) + timedelta(minutes=minutes)


def raw_close(d):
    base = 400.0 + DAYS.index(d)
    return base / 4 if d >= EX else base


def bars(adjusted_as_of=None, ratio=4.0):
    """AAPL raw bars, and split-adjusted ones as Alpaca serves them once it knows the split."""
    raw = [TI.bar(bar_ts(d), close=raw_close(d)) for d in DAYS]
    if adjusted_as_of is None or adjusted_as_of < EX:
        return {"AAPL": raw}, {"AAPL": raw}
    adj = [TI.bar(bar_ts(d), close=raw_close(d) / ratio, volume=1000.0 * ratio) if d < EX else
           TI.bar(bar_ts(d), close=raw_close(d)) for d in DAYS]
    return {"AAPL": raw}, {"AAPL": _scaled(raw, adj)}


def _scaled(raw, adj):
    """Adjusted bars: every price field scaled by the same ratio as the close (volume inversely)."""
    out = []
    for r, a in zip(raw, adj):
        k = a.close / r.close
        out.append(TI.SimpleNamespace(timestamp=r.timestamp, open=r.open * k, high=r.high * k, low=r.low * k,
                                      close=r.close * k, volume=r.volume / k, vwap=r.vwap, trade_count=r.trade_count))
    return out


class FakeCorporate:
    def __init__(self, data):
        self.data, self.requests = data, []

    def get_corporate_actions(self, req):
        self.requests.append(req)
        return self.data


SPLIT = {"forward_splits": [{"symbol": "AAPL", "cusip": "037833100", "new_rate": 4, "old_rate": 1,
                             "process_date": "2026-11-11", "ex_date": "2026-11-11", "record_date": "2026-11-10"}],
         "cash_dividends": [{"symbol": "AAPL", "rate": 0.26, "ex_date": "2026-11-09", "process_date": "2026-11-09"}],
         "cash_mergers": [{"acquirer_symbol": "AAPL", "acquiree_symbol": "ZZZZ", "rate": 10,
                           "effective_date": "2026-11-12", "process_date": "2026-11-12"}]}


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "ca.db")
    yield c
    c.close()


def backfill(conn, now):
    raw, split = bars(None)
    cl = ingest.Clients(TI.FakeTrading(), TI.FakeStock(raw, split), TI.FakeCrypto({}))
    assert ingest.run(conn, cl, data_checks=False, start_date=DAYS[0], now=lambda: now, sleep=lambda s: None,
                      equity_symbols=["AAPL"], crypto_symbols=()) == 0


# --- fetch and store ----------------------------------------------------------------

def test_normalise_keeps_affected_universe_stocks_only():
    rows = ca.normalise(SPLIT, ["AAPL", "SPY"])
    assert [(r["ca_type"], r["symbol"], r["ex_date"], r["old_rate"], r["new_rate"]) for r in rows] == \
        [("forward_split", "AAPL", "2026-11-11", 1.0, 4.0)]              # no dividend; the acquirer isn't affected
    assert ca.normalise({"cash_mergers": SPLIT["cash_mergers"]}, ["ZZZZ"])[0]["ca_type"] == "cash_merger"


def test_fetch_stores_versions_only_when_content_changes(conn):
    t = datetime(2026, 11, 10, 22, 30, tzinfo=UTC)
    fake = FakeCorporate(SPLIT)
    assert ca.fetch(conn, fake, ["AAPL"], lambda: t, lambda s: None)["inserted"] == 1
    assert ca.fetch(conn, fake, ["AAPL"], lambda: t + timedelta(days=1), lambda s: None)["inserted"] == 0
    req = fake.requests[0]
    assert req.symbols == ["AAPL"] and "cash_dividend" not in [x.value for x in req.types]
    assert (req.start, req.end) == (date(2026, 10, 11), date(2026, 12, 10))
    changed = json.loads(json.dumps(SPLIT))
    changed["forward_splits"][0]["payable_date"] = "2026-11-10"
    assert ca.fetch(conn, FakeCorporate(changed), ["AAPL"], lambda: t + timedelta(days=2), lambda s: None)["inserted"] == 1
    assert conn.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0] == 2


def test_ingest_fetches_actions_and_a_failure_doesnt_fail_it(conn):
    class Broken:
        def get_corporate_actions(self, req):
            raise ValueError("boom")

    now = after_close(DAYS[-1])
    raw, split = bars(None)
    cl = ingest.Clients(TI.FakeTrading(), TI.FakeStock(raw, split), TI.FakeCrypto({}), Broken())
    assert ingest.run(conn, cl, data_checks=False, start_date=DAYS[0], now=lambda: now, sleep=lambda s: None,
                      equity_symbols=["AAPL"], crypto_symbols=()) == 0
    hb = conn.execute("SELECT event, detail FROM heartbeat WHERE job = 'corporate_actions'").fetchone()
    assert hb["event"] == "error" and "boom" in hb["detail"]
    cl = ingest.Clients(TI.FakeTrading(), TI.FakeStock(raw, split), TI.FakeCrypto({}), FakeCorporate(SPLIT))
    assert ingest.run(conn, cl, data_checks=False, days=5, now=lambda: now + timedelta(minutes=1),
                      sleep=lambda s: None, equity_symbols=["AAPL"], crypto_symbols=()) == 0
    assert conn.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0] == 1


# --- the history re-fetch -----------------------------------------------------------------

def test_split_refetches_the_whole_history_as_new_versions(conn):
    before = after_close(date(2026, 11, 10))                    # Tue: the day before the ex-date
    backfill(conn, before)
    assert ca.stale_symbols(conn, ["AAPL"], before) == []
    # Wed 11 Nov, ex-date: Alpaca has adjusted; a 5-day ingest alone would leave older bars on r = 1
    t = after_close(EX)
    raw, split = bars(adjusted_as_of=EX)
    cl = ingest.Clients(TI.FakeTrading(), TI.FakeStock(raw, split), TI.FakeCrypto({}))
    stats = ingest.Stats()
    assert ingest.run(conn, cl, data_checks=False, days=5, now=lambda: t, sleep=lambda s: None,
                      equity_symbols=["AAPL"], crypto_symbols=()) == 0
    reqs = cl.stock.requests
    assert len(reqs) == 4                                       # 5-day window (raw, split) + one re-fetch
    assert TI.utc(reqs[2].start).date() == DAYS[0]
    assert ca.stale_symbols(conn, ["AAPL"], t) == []
    # every pre-split bar has a new version at this fetch, adjusted by 1/4
    latest = {r["bar_date"]: r for r in db.prices_as_of(conn, db.utc_ts(t), ["AAPL"])}
    old = date(2026, 10, 1).isoformat()
    assert latest[old]["adj_close"] == pytest.approx(latest[old]["close"] / 4)
    assert latest[EX.isoformat()]["adj_close"] == latest[EX.isoformat()]["close"]
    assert checks.find_splits(conn, ["AAPL"], DAYS[0], t) == [
        {"symbol": "AAPL", "date": EX.isoformat(), "factor": "4:1", "ratio_before": 0.25, "ratio_after": 1.0}]
    _, problems = checks.analyse_splits("AAPL", "us_equity",
                                        checks.load_bars(conn, ["AAPL"], DAYS[0], t)["AAPL"], DAYS[0])
    assert problems == []
    # rule 1: as of the day before, the old versions are what is visible
    seen = {r["bar_date"]: r for r in db.prices_as_of(conn, db.utc_ts(before), ["AAPL"])}
    assert seen[old]["adj_close"] == seen[old]["close"]
    # a second run is a no-op: nothing stale, no extra fetch
    cl2 = ingest.Clients(TI.FakeTrading(), TI.FakeStock(raw, split), TI.FakeCrypto({}))
    n = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    assert ingest.run(conn, cl2, data_checks=False, days=5, now=lambda: t + timedelta(minutes=5),
                      sleep=lambda s: None, equity_symbols=["AAPL"], crypto_symbols=()) == 0
    assert len(cl2.stock.requests) == 2
    assert conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == n


def test_no_refetch_while_alpaca_hasnt_adjusted_yet(conn):
    backfill(conn, after_close(date(2026, 11, 10)))
    t = after_close(EX)
    raw, split = bars(adjusted_as_of=None)                     # the lag case: raw jumps, nothing adjusted
    cl = ingest.Clients(TI.FakeTrading(), TI.FakeStock(raw, split), TI.FakeCrypto({}))
    assert ingest.run(conn, cl, data_checks=False, days=5, now=lambda: t, sleep=lambda s: None,
                      equity_symbols=["AAPL"], crypto_symbols=()) == 0
    assert len(cl.stock.requests) == 2
    assert ca.price_step(conn, "AAPL", EX.isoformat(), t) == pytest.approx(1.0)


# --- verdicts -------------------------------------------------------------------------

def add_action(conn, ca_type="forward_split", sym="AAPL", ex=EX, old=1.0, new=4.0, at=None, key_extra=""):
    at = at or after_close(ex, 60)
    conn.execute("""INSERT INTO corporate_actions (ca_key, ca_type, symbol, ex_date, old_rate, new_rate, raw,
                    fetched_at) VALUES (?, ?, ?, ?, ?, ?, '{}', ?)""",
                 (f"{ca_type}|{sym}|{ex}{key_extra}", ca_type, sym, ex.isoformat(), old, new, db.utc_ts(at)))
    conn.commit()


def adjusted_world(conn, ratio=4.0):
    backfill(conn, after_close(date(2026, 11, 10)))
    t = after_close(EX)
    raw, split = bars(adjusted_as_of=EX, ratio=ratio)
    cl = ingest.Clients(TI.FakeTrading(), TI.FakeStock(raw, split), TI.FakeCrypto({}))
    assert ingest.run(conn, cl, data_checks=False, days=5, now=lambda: t, sleep=lambda s: None,
                      equity_symbols=["AAPL"], crypto_symbols=()) == 0
    return t


def test_confirmed_split(conn):
    t = adjusted_world(conn)
    add_action(conn)
    rows = ca.review(conn, EX, ["AAPL"], t + timedelta(minutes=5))
    assert [(r["event"], r["ratio"]) for r in rows] == [("split", 4.0)]
    assert ca.review(conn, EX, ["AAPL"], t + timedelta(minutes=6)) == []          # unchanged: no new row
    assert ca.frozen(conn, EX, t + timedelta(minutes=6)) == {}


@pytest.mark.parametrize("case", ["no_announcement", "ratio_mismatch", "spin_off", "two_actions", "no_ratio"])
def test_ambiguous_cases_freeze(conn, case):
    t = adjusted_world(conn)
    if case == "ratio_mismatch":
        add_action(conn, new=2.0)
    elif case == "spin_off":
        add_action(conn, ca_type="spin_off", old=None, new=None)
    elif case == "two_actions":
        add_action(conn)
        add_action(conn, ca_type="stock_dividend", old=None, new=None)
    elif case == "no_ratio":
        add_action(conn, old=0.0)
    rows = ca.review(conn, EX, ["AAPL"], t + timedelta(minutes=5))
    assert [r["event"] for r in rows] == ["freeze"]
    f = ca.frozen(conn, EX, t + timedelta(minutes=5))
    assert list(f) == ["AAPL"]
    assert ca.frozen_mark(conn, "AAPL", EX.isoformat(), t) == raw_close(date(2026, 11, 10))


def test_lag_case_freezes_then_lifts_on_the_confirming_fetch(conn):
    backfill(conn, after_close(date(2026, 11, 10)))
    add_action(conn)                                           # the announcement is known on the ex-date
    t = after_close(EX)
    raw, split = bars(adjusted_as_of=None)
    cl = ingest.Clients(TI.FakeTrading(), TI.FakeStock(raw, split), TI.FakeCrypto({}))
    assert ingest.run(conn, cl, data_checks=False, days=5, now=lambda: t, sleep=lambda s: None,
                      equity_symbols=["AAPL"], crypto_symbols=()) == 0
    rows = ca.review(conn, EX, ["AAPL"], t + timedelta(minutes=5))
    assert [r["event"] for r in rows] == ["freeze"] and "not confirmed" in rows[0]["reason"]
    # Thu 12 Nov: Alpaca has adjusted; the ingest re-fetches; the review lifts the freeze with the split
    nxt = date(2026, 11, 12)
    t2 = after_close(nxt)
    raw, split = bars(adjusted_as_of=nxt)
    cl = ingest.Clients(TI.FakeTrading(), TI.FakeStock(raw, split), TI.FakeCrypto({}))
    assert ingest.run(conn, cl, data_checks=False, days=5, now=lambda: t2, sleep=lambda s: None,
                      equity_symbols=["AAPL"], crypto_symbols=()) == 0
    rows = ca.review(conn, nxt, ["AAPL"], t2 + timedelta(minutes=5))
    assert [(r["event"], r["ex_date"], r["ratio"]) for r in rows] == [("split", EX.isoformat(), 4.0)]
    e = ca.latest_event(conn, "AAPL", EX.isoformat())
    assert e["corrects_id"] is not None and ca.frozen(conn, nxt, t2 + timedelta(minutes=5)) == {}


def test_manual_resolution_is_a_new_entry_and_review_leaves_it(conn):
    t = adjusted_world(conn)
    add_action(conn, ca_type="spin_off", old=None, new=None)
    ca.review(conn, EX, ["AAPL"], t + timedelta(minutes=5))
    freeze = ca.latest_event(conn, "AAPL", EX.isoformat())
    sid = ca.resolve(conn, "AAPL", EX.isoformat(), t + timedelta(minutes=10), 4.0, "checked by hand")
    row = conn.execute("SELECT * FROM symbol_events WHERE symbol_event_id = ?", (sid,)).fetchone()
    assert (row["event"], row["source"], row["corrects_id"]) == ("split", "manual", freeze["symbol_event_id"])
    assert ca.review(conn, EX, ["AAPL"], t + timedelta(minutes=15)) == []
    with pytest.raises(ca.CorporateActionError):
        ca.resolve(conn, "AAPL", EX.isoformat(), t + timedelta(minutes=20), None)


# --- name changes (v1.18 amended) -----------------------------------------------------

XOM_2026_07_02 = {"name_changes": [{"id": "71daae23-2985-4549-870e-a6d678474b6b", "new_cusip": "30233Q108",
                                    "new_symbol": "XOM", "old_cusip": "30231G102", "old_symbol": "XOM",
                                    "process_date": "2026-07-02"}]}      # as Alpaca returned it


def xom_world(conn, step=1.0):
    """XOM bars to Wed 1 Jul 2026 (raw / adj = step on the last), then 2 Jul."""
    for i, d in enumerate(TI.weekdays(date(2026, 6, 1), date(2026, 7, 2))):
        b = TI.bar(bar_ts(d), close=100.0 + i)
        a = b if d == date(2026, 7, 2) or step == 1.0 else TI.SimpleNamespace(**{**vars(b), **{
            k: getattr(b, k) / step for k in ("open", "high", "low", "close")}, "volume": b.volume * step})
        rows = [ingest.make_row("XOM", "us_equity", d.isoformat(), b, a, "iex")]
        ingest.store(conn, rows, db.utc_ts(after_close(d)), ingest.Stats())


def test_replay_of_xoms_cusip_change_is_logged_only(conn):
    xom_world(conn)
    t = after_close(date(2026, 7, 2))
    assert ca.fetch(conn, FakeCorporate(XOM_2026_07_02), ["XOM"], lambda: t, lambda s: None)["inserted"] == 1
    row = conn.execute("SELECT * FROM corporate_actions").fetchone()
    assert (row["ca_type"], row["symbol"], row["ex_date"]) == ("name_change", "XOM", "2026-07-02")
    noted = []
    assert ca.review(conn, date(2026, 7, 2), ["XOM"], t + timedelta(minutes=5), noted) == []
    assert conn.execute("SELECT COUNT(*) FROM symbol_events").fetchone()[0] == 0
    assert ca.frozen(conn, date(2026, 7, 2), t + timedelta(minutes=5)) == {}
    assert [(n["symbol"], n["ex_date"], n["ca_type"], n["ca_id"]) for n in noted] == \
        [("XOM", "2026-07-02", "name_change", row["ca_id"])]


def test_change_of_ticker_freezes(conn):
    xom_world(conn)
    t = after_close(date(2026, 7, 2))
    renamed = {"name_changes": [{**XOM_2026_07_02["name_changes"][0], "new_symbol": "XOMX"}]}
    ca.fetch(conn, FakeCorporate(renamed), ["XOM"], lambda: t, lambda s: None)
    rows = ca.review(conn, date(2026, 7, 2), ["XOM"], t + timedelta(minutes=5))
    assert [(r["event"], r["reason"]) for r in rows] == [("freeze", "name_change on 2026-07-02")]


def test_same_ticker_rename_doesnt_hide_an_unexplained_price_step(conn):
    xom_world(conn, step=2.0)                          # the bars before 2 Jul show a 2x step, no split announced
    t = after_close(date(2026, 7, 2))
    ca.fetch(conn, FakeCorporate(XOM_2026_07_02), ["XOM"], lambda: t, lambda s: None)
    noted = []
    rows = ca.review(conn, date(2026, 7, 2), ["XOM"], t + timedelta(minutes=5), noted)
    assert [r["event"] for r in rows] == ["freeze"] and "no announcement" in rows[0]["reason"]
    assert len(noted) == 1
