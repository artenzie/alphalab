"""Ingestion tests with fake Alpaca clients (no network)."""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alpaca.common.exceptions import APIError  # noqa: E402

from data import db, ingest  # noqa: E402

UTC = timezone.utc
# Fri 13 Nov 2026: equity bar stamped 05:00Z (midnight ET), crypto bar at 00:00Z.
EQ_TS = datetime(2026, 11, 13, 5, tzinfo=UTC)
CR_TS = datetime(2026, 11, 13, 0, tzinfo=UTC)
AFTER_ALL = datetime(2026, 11, 14, 1, tzinfo=UTC)  # both bars closed


def bar(ts, close=100.0, **kw):
    v = dict(timestamp=ts, open=close - 1, high=close + 2, low=close - 2, close=close,
             volume=1000.0, vwap=close - 0.5, trade_count=42)
    v.update(kw)
    return SimpleNamespace(**v)


def api_error(status):
    resp = SimpleNamespace(status_code=status)
    return APIError(json.dumps({"code": status, "message": "boom"}),
                    SimpleNamespace(response=resp, request=None))


def utc(dt):
    """alpaca-py stores request times as naive UTC."""
    return None if dt is None else (dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt)


def in_window(bars, req):
    """Bars whose timestamp falls in [req.start, req.end], like Alpaca does."""
    start, end = utc(req.start), utc(req.end)
    return [b for b in bars if (start is None or b.timestamp >= start)
            and (end is None or b.timestamp <= end)]


def weekdays(start, end):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


class FakeTrading:
    def __init__(self, untradable=(), sessions=None, early_closes=()):
        self.untradable = set(untradable)
        self.sessions = sessions  # None: every weekday is a session
        self.early_closes = set(early_closes)  # these close at 13:00 ET
        self.calendar_requests = []

    def get_asset(self, sym):
        return SimpleNamespace(symbol=sym, tradable=sym not in self.untradable, status="AssetStatus.ACTIVE")

    def get_calendar(self, req):
        self.calendar_requests.append(req)
        days = weekdays(req.start, req.end) if self.sessions is None else \
            [d for d in self.sessions if req.start <= d <= req.end]
        # Like alpaca-py: open/close as ET wall-clock times.
        return [SimpleNamespace(date=d, open="09:30", close="13:00" if d in self.early_closes else "16:00")
                for d in days]


class FakeStock:
    def __init__(self, raw, split, failures=(), fail_after=None):
        self.data = {"raw": raw, "split": split}
        self.failures = list(failures)
        self.fail_after = fail_after  # raise HTTP 403 on every call after this many
        self.calls = 0
        self.requests = []

    def get_stock_bars(self, req):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        if self.fail_after is not None and self.calls > self.fail_after:
            raise api_error(403)
        self.requests.append(req)
        data = self.data[req.adjustment.value]
        return SimpleNamespace(data={s: in_window(b, req) for s, b in data.items()})


class FakeCrypto:
    def __init__(self, data):
        self.data = data
        self.requests = []

    def get_crypto_bars(self, req):
        self.requests.append(req)
        return SimpleNamespace(data={s: in_window(b, req) for s, b in self.data.items()})


def clients(raw=None, split=None, crypto=None, failures=(), untradable=(), sessions=None, fail_after=None,
            early_closes=()):
    raw = {"AAPL": [bar(EQ_TS)]} if raw is None else raw
    split = raw if split is None else split
    crypto = {"BTC/USD": [bar(CR_TS, close=70000.0)]} if crypto is None else crypto
    return ingest.Clients(FakeTrading(untradable, sessions, early_closes), FakeStock(raw, split, failures, fail_after),
                          FakeCrypto(crypto))


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "test.db")
    yield c
    c.close()


def do_run(conn, cl, now=AFTER_ALL, sleeps=None):
    sleeps = [] if sleeps is None else sleeps
    return ingest.run(conn, cl, data_checks=False, days=5, now=lambda: now, sleep=sleeps.append,
                      equity_symbols=["AAPL"], crypto_symbols=("BTC/USD",))


class Clock:
    """now() that advances 1 ms per call, so successive runs get distinct fetched_at."""

    def __init__(self, start):
        self.t = start

    def __call__(self):
        self.t = self.t.replace(microsecond=self.t.microsecond + 1000)
        return self.t


def prices(conn):
    return conn.execute("SELECT * FROM prices ORDER BY symbol, fetched_at").fetchall()


def events(conn):
    return [(r["event"], json.loads(r["detail"])) for r in
            conn.execute("SELECT * FROM heartbeat ORDER BY heartbeat_id")]


# --- storage and merging ---------------------------------------------------

def test_raw_and_split_merge_into_one_row(conn):
    raw = {"AAPL": [bar(EQ_TS, close=400.0)]}
    split = {"AAPL": [bar(EQ_TS, close=100.0, volume=4000.0)]}
    assert do_run(conn, clients(raw, split)) == 0
    (aapl,) = [r for r in prices(conn) if r["symbol"] == "AAPL"]
    assert (aapl["close"], aapl["adj_close"]) == (400.0, 100.0)
    assert (aapl["volume"], aapl["adj_volume"]) == (1000.0, 4000.0)
    assert aapl["bar_date"] == "2026-11-13" and aapl["bar_ts"] == "2026-11-13T05:00:00.000Z"
    assert aapl["feed"] == "iex" and aapl["asset_class"] == "us_equity"
    (btc,) = [r for r in prices(conn) if r["symbol"] == "BTC/USD"]
    assert btc["asset_class"] == "crypto" and btc["feed"] == "crypto"
    assert btc["adj_close"] == btc["close"] == 70000.0


def test_rerun_with_same_data_inserts_nothing(conn):
    clock = Clock(AFTER_ALL)
    for _ in range(3):
        assert ingest.run(conn, clients(), data_checks=False, days=5, now=clock, sleep=lambda s: None,
                          equity_symbols=["AAPL"], crypto_symbols=("BTC/USD",)) == 0
    assert len(prices(conn)) == 2
    finished = [d for e, d in events(conn) if e == "finished"]
    assert [f["totals"]["inserted"] for f in finished] == [2, 0, 0]
    assert finished[-1]["totals"]["unchanged"] == 2


def test_changed_values_create_new_version(conn):
    clock = Clock(AFTER_ALL)
    kw = dict(days=5, now=clock, sleep=lambda s: None, equity_symbols=["AAPL"], crypto_symbols=("BTC/USD",))
    raw = {"AAPL": [bar(EQ_TS, close=400.0)]}
    ingest.run(conn, clients(raw, raw), data_checks=False, **kw)
    first_fetch = prices(conn)[0]["fetched_at"]
    # A 4:1 split is applied later: raw unchanged, adjusted values change.
    ingest.run(conn, clients(raw, {"AAPL": [bar(EQ_TS, close=100.0)]}), data_checks=False, **kw)
    aapl = [r for r in prices(conn) if r["symbol"] == "AAPL"]
    assert [r["adj_close"] for r in aapl] == [400.0, 100.0]
    # Point-in-time: a cutoff before the second fetch still sees the old version.
    old = db.prices_as_of(conn, first_fetch, ["AAPL"])
    new = db.prices_as_of(conn, "2026-11-15T00:00:00.000Z", ["AAPL"])
    assert old[0]["adj_close"] == 400.0 and new[0]["adj_close"] == 100.0


def test_raw_split_mismatch_is_an_error(conn):
    raw = {"AAPL": [bar(EQ_TS)]}
    assert do_run(conn, clients(raw, {"AAPL": []})) == 1
    assert prices(conn) == []


# --- unclosed bars ---------------------------------------------------------

def test_equity_bar_before_close_rejected(conn):
    # 15:59 ET on Fri 13 Nov (EST, UTC-5) = 20:59Z
    assert do_run(conn, clients(crypto={}), now=datetime(2026, 11, 13, 20, 59, tzinfo=UTC)) == 0
    assert prices(conn) == []
    fin = events(conn)[-1][1]
    assert fin["per_symbol"]["AAPL"]["rejected_unclosed"] == 1


def test_equity_bar_at_close_accepted(conn):
    assert do_run(conn, clients(crypto={}), now=datetime(2026, 11, 13, 21, 0, tzinfo=UTC)) == 0
    assert [r["symbol"] for r in prices(conn)] == ["AAPL"]


# Fri 27 Nov 2026: early close at 13:00 ET (EST, UTC-5) = 18:00Z. Protocol §3
# fetches at close + 1:30 = 14:30 ET = 19:30Z, so the bar must be accepted then.
BLACK_FRIDAY_TS = datetime(2026, 11, 27, 5, tzinfo=UTC)


def early_close_clients():
    return clients(raw={"AAPL": [bar(BLACK_FRIDAY_TS)]}, crypto={}, early_closes={date(2026, 11, 27)})


def test_early_close_bar_accepted_at_1430_et(conn):
    assert do_run(conn, early_close_clients(), now=datetime(2026, 11, 27, 19, 30, tzinfo=UTC)) == 0
    assert [(r["symbol"], r["bar_date"]) for r in prices(conn)] == [("AAPL", "2026-11-27")]
    assert events(conn)[-1][1]["coverage"]["AAPL"]["unexpected"] == []


def test_early_close_bar_rejected_before_1300_et(conn):
    assert do_run(conn, early_close_clients(), now=datetime(2026, 11, 27, 17, 59, tzinfo=UTC)) == 0
    assert prices(conn) == []
    assert events(conn)[-1][1]["per_symbol"]["AAPL"]["rejected_unclosed"] == 1


def test_close_comes_from_cached_calendar(conn):
    do_run(conn, early_close_clients(), now=datetime(2026, 11, 27, 19, 30, tzinfo=UTC))
    row = conn.execute("SELECT close_ts FROM market_sessions WHERE session_date = '2026-11-27'").fetchone()
    assert row["close_ts"] == "2026-11-27T18:00:00.000Z"


def test_bar_on_a_day_with_no_session_uses_1600_et(conn):
    # Wed 11 Nov dropped as a fake holiday; a bar dated that day waits for 16:00 ET.
    ts = datetime(2026, 11, 11, 5, tzinfo=UTC)
    sessions = [x for x in weekdays(date(2024, 11, 1), date(2027, 12, 31)) if x != date(2026, 11, 11)]
    cl = clients(raw={"AAPL": [bar(ts)]}, crypto={}, sessions=sessions)
    assert do_run(conn, cl, now=datetime(2026, 11, 11, 20, 59, tzinfo=UTC)) == 0
    assert prices(conn) == []
    assert do_run(conn, cl, now=datetime(2026, 11, 11, 21, 0, tzinfo=UTC)) == 0
    assert len(prices(conn)) == 1


def test_calendar_unavailable_stops_run(conn):
    cl = clients(crypto={})
    cl.trading.get_calendar = lambda req: (_ for _ in ()).throw(api_error(403))
    assert do_run(conn, cl) == 1
    assert prices(conn) == []
    assert events(conn)[-1][0] == "error"


def test_crypto_bar_before_midnight_utc_rejected(conn):
    now = datetime(2026, 11, 13, 23, 59, 59, tzinfo=UTC)
    assert do_run(conn, clients(raw={}), now=now) == 0
    assert prices(conn) == []
    assert events(conn)[-1][1]["per_symbol"]["BTC/USD"]["rejected_unclosed"] == 1


def test_crypto_bar_at_midnight_utc_accepted(conn):
    assert do_run(conn, clients(raw={}), now=datetime(2026, 11, 14, 0, 0, tzinfo=UTC)) == 0
    assert [r["symbol"] for r in prices(conn)] == ["BTC/USD"]


def test_crypto_bar_not_at_midnight_utc_is_an_error(conn):
    cl = clients(raw={}, crypto={"BTC/USD": [bar(datetime(2026, 11, 13, 6, tzinfo=UTC))]})
    assert do_run(conn, cl) == 1
    assert "00:00 UTC" in events(conn)[-1][1]["error"]


# --- crypto pair check -----------------------------------------------------

def test_untradable_crypto_pair_stops_run(conn):
    assert do_run(conn, clients(untradable={"BTC/USD"})) == 1
    assert prices(conn) == []
    ev, detail = events(conn)[-1]
    assert ev == "error" and "BTC/USD" in detail["error"]


# --- retries ---------------------------------------------------------------

def test_retry_then_success(conn):
    cl = clients(failures=[requests.exceptions.ConnectionError("reset"), api_error(503)])
    sleeps = []
    assert do_run(conn, cl, sleeps=sleeps) == 0
    assert sleeps == list(ingest.RETRY_DELAYS)
    assert cl.stock.calls == 4  # 2 failures + raw + split
    assert len(prices(conn)) == 2
    assert [e for e, _ in events(conn)] == ["started", "finished"]


def test_retry_then_fail(conn):
    cl = clients(failures=[api_error(503), requests.exceptions.Timeout("slow"), api_error(429)])
    sleeps = []
    assert do_run(conn, cl, sleeps=sleeps) == 1
    assert sleeps == list(ingest.RETRY_DELAYS)
    assert prices(conn) == []
    ev, detail = events(conn)[-1]
    assert ev == "error" and "gave up after 3 attempts" in detail["error"]


def test_non_retryable_error_fails_immediately(conn):
    cl = clients(failures=[api_error(403)])
    sleeps = []
    assert do_run(conn, cl, sleeps=sleeps) == 1
    assert sleeps == [] and cl.stock.calls == 1
    assert "HTTP 403" in events(conn)[-1][1]["error"]


# --- heartbeat -------------------------------------------------------------

def test_heartbeat_started_and_finished_with_counts(conn):
    assert do_run(conn, clients()) == 0
    rows = conn.execute("SELECT run_id, job, event FROM heartbeat ORDER BY heartbeat_id").fetchall()
    assert [r["event"] for r in rows] == ["started", "finished"]
    assert len({r["run_id"] for r in rows}) == 1 and rows[0]["job"] == "fetch_prices"
    fin = events(conn)[-1][1]
    assert fin["totals"] == {"fetched": 2, "inserted": 2, "unchanged": 0, "rejected_unclosed": 0}


# --- backfill: chunking ----------------------------------------------------

def utc_dt(*a):
    return datetime(*a, tzinfo=UTC)


def test_chunk_ranges_split_by_year_without_overlap():
    chunks = ingest.chunk_ranges(utc_dt(2024, 11, 1), utc_dt(2026, 11, 14, 22))
    assert chunks == [
        (utc_dt(2024, 11, 1), utc_dt(2024, 12, 31, 23, 59, 59)),
        (utc_dt(2025, 1, 1), utc_dt(2025, 12, 31, 23, 59, 59)),
        (utc_dt(2026, 1, 1), utc_dt(2026, 11, 14, 22)),
    ]
    for (_, end), (nxt, _) in zip(chunks, chunks[1:]):
        assert end < nxt


def test_chunk_ranges_within_one_year_and_empty():
    assert ingest.chunk_ranges(utc_dt(2026, 11, 9), utc_dt(2026, 11, 14)) == \
        [(utc_dt(2026, 11, 9), utc_dt(2026, 11, 14))]
    assert ingest.chunk_ranges(utc_dt(2026, 11, 14), utc_dt(2026, 11, 14)) == []
    assert ingest.chunk_ranges(utc_dt(2026, 11, 15), utc_dt(2026, 11, 14)) == []


# Bars on both sides of each year boundary; equity at 05:00Z (midnight ET).
BF_EQ = [utc_dt(2024, 11, 1, 5), utc_dt(2024, 12, 31, 5), utc_dt(2025, 1, 2, 5),
         utc_dt(2025, 12, 31, 5), utc_dt(2026, 1, 2, 5), utc_dt(2026, 11, 13, 5)]
BF_CR = [utc_dt(2024, 11, 1), utc_dt(2024, 12, 31), utc_dt(2025, 1, 1), utc_dt(2026, 1, 1),
         utc_dt(2026, 11, 13)]


def backfill(conn, cl, now=AFTER_ALL, sleeps=None):
    sleeps = [] if sleeps is None else sleeps
    return ingest.run(conn, cl, data_checks=False, now=now if callable(now) else (lambda: now), sleep=sleeps.append,
                      equity_symbols=["AAPL"], crypto_symbols=("BTC/USD",),
                      start_date=date(2024, 11, 1))


def bf_clients(**kw):
    return clients(raw={"AAPL": [bar(t) for t in BF_EQ]},
                   crypto={"BTC/USD": [bar(t, close=70000.0) for t in BF_CR]}, **kw)


def test_backfill_fetches_each_chunk_and_stores_everything(conn):
    cl = bf_clients()
    sleeps = []
    assert backfill(conn, cl, sleeps=sleeps) == 0
    # 3 yearly chunks x (raw + split) stock requests, 3 crypto requests
    assert len(cl.stock.requests) == 6 and len(cl.crypto.requests) == 3
    starts = [utc(r.start) for r in cl.crypto.requests]
    ends = [utc(r.end) for r in cl.crypto.requests]
    assert starts == [utc_dt(2024, 11, 1), utc_dt(2025, 1, 1), utc_dt(2026, 1, 1)]
    assert ends[:2] == [utc_dt(2024, 12, 31, 23, 59, 59), utc_dt(2025, 12, 31, 23, 59, 59)]
    assert sleeps == [ingest.CHUNK_PAUSE] * 2  # pause between chunks only, no retries
    stored = {(r["symbol"], r["bar_date"]) for r in prices(conn)}
    assert len(prices(conn)) == len(stored) == len(BF_EQ) + len(BF_CR)  # each bar once
    started = events(conn)[0][1]
    assert started["mode"] == "backfill" and len(started["chunks"]) == 3
    assert len(events(conn)[-1][1]["fetched_at_by_chunk"]) == 3


def test_backfill_rerun_inserts_nothing(conn):
    clock = Clock(AFTER_ALL)
    assert backfill(conn, bf_clients(), now=clock) == 0
    assert backfill(conn, bf_clients(), now=clock) == 0
    finished = [d for e, d in events(conn) if e == "finished"]
    assert [f["totals"]["inserted"] for f in finished] == [11, 0]


def test_backfill_failure_in_later_chunk_keeps_earlier_chunks(conn):
    cl = bf_clients(fail_after=4)  # chunks 1 and 2 succeed, chunk 3 gets HTTP 403
    assert backfill(conn, cl) == 1
    dates = sorted(r["bar_date"] for r in prices(conn))
    assert dates and max(dates) < "2026-01-01"
    ev, detail = events(conn)[-1]
    assert ev == "error" and detail["chunks_stored"] == 2 and "HTTP 403" in detail["error"]


def test_start_after_now_is_an_error(conn):
    rc = ingest.run(conn, clients(), data_checks=False, now=lambda: AFTER_ALL, sleep=lambda s: None,
                    equity_symbols=["AAPL"], crypto_symbols=("BTC/USD",), start_date=date(2026, 11, 15))
    assert rc == 1 and "not before now" in events(conn)[-1][1]["error"]


# --- coverage / gap report -------------------------------------------------

def d(s):
    return date.fromisoformat(s)


def test_date_ranges_group_consecutive_expected_days():
    expected = ["2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10", "2026-11-12"]
    # Fri 6 and Mon 9 are consecutive sessions -> one range; 12 is separate
    assert ingest.date_ranges(["2026-11-06", "2026-11-09", "2026-11-12"], expected) == \
        ["2026-11-06..2026-11-09", "2026-11-12"]


def test_symbol_coverage_no_gaps():
    exp = ["2026-11-09", "2026-11-10", "2026-11-11"]
    r = ingest.symbol_coverage(exp, exp)
    assert (r["first"], r["last"], r["bars"]) == ("2026-11-09", "2026-11-11", 3)
    assert not r["late_start"] and r["gaps"] == [] and r["unexpected"] == []


def test_symbol_coverage_late_start_middle_gap_and_missing_tail():
    exp = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]
    r = ingest.symbol_coverage(["2026-11-03", "2026-11-05"], exp)
    assert r["late_start"] and r["missing_before_first"] == 1
    assert r["gaps"] == ["2026-11-04", "2026-11-06"]  # middle gap and missing last day
    assert r["gap_ranges"] == ["2026-11-04", "2026-11-06"]


def test_symbol_coverage_no_bars_at_all():
    r = ingest.symbol_coverage([], ["2026-11-02"])
    assert r["first"] is None and r["bars"] == 0 and r["late_start"] and r["gaps"] == []


def test_expected_equity_days_uses_calendar_and_closed_rule():
    # Mon 9 .. Fri 13 Nov 2026; Wed 11 dropped as a fake holiday
    sessions = [d("2026-11-09"), d("2026-11-10"), d("2026-11-12"), d("2026-11-13")]
    cl = clients(sessions=sessions)
    before_close = utc_dt(2026, 11, 13, 20, 59)  # 15:59 ET Fri
    days = ingest.expected_equity_days(cl, d("2026-11-09"), before_close, lambda s: None)
    assert days == ["2026-11-09", "2026-11-10", "2026-11-12"]
    after = ingest.expected_equity_days(cl, d("2026-11-09"), AFTER_ALL, lambda s: None)
    assert after[-1] == "2026-11-13"


def test_expected_crypto_days_are_every_closed_calendar_day():
    days = ingest.expected_crypto_days(d("2026-11-12"), utc_dt(2026, 11, 14, 23, 59))
    assert days == ["2026-11-12", "2026-11-13"]  # 14 Nov not closed yet
    assert ingest.expected_crypto_days(d("2026-11-14"), utc_dt(2026, 11, 14, 12)) == []


def test_run_reports_gaps_not_weekends_or_holidays(conn):
    # Sessions Mon 2 .. Fri 13 Nov except Wed 11 (holiday). AAPL misses Thu 5.
    sessions = [x for x in weekdays(d("2026-11-02"), d("2026-11-13")) if x != d("2026-11-11")]
    eq = [bar(datetime(x.year, x.month, x.day, 5, tzinfo=UTC)) for x in sessions if x != d("2026-11-05")]
    # BTC every day 2..13 Nov except Sun 8
    cr_days = [d("2026-11-02") + timedelta(days=i) for i in range(12)]
    cr = [bar(datetime(x.year, x.month, x.day, tzinfo=UTC)) for x in cr_days if x != d("2026-11-08")]
    cl = clients(raw={"AAPL": eq}, crypto={"BTC/USD": cr}, sessions=sessions)
    rc = ingest.run(conn, cl, data_checks=False, now=lambda: AFTER_ALL, sleep=lambda s: None, equity_symbols=["AAPL"],
                    crypto_symbols=("BTC/USD",), start_date=d("2026-11-02"))
    assert rc == 0
    cov = events(conn)[-1][1]["coverage"]
    assert cov["AAPL"]["gap_ranges"] == ["2026-11-05"]  # weekends and the holiday aren't gaps
    assert cov["AAPL"]["bars"] == 8 and cov["AAPL"]["expected"] == 9
    assert cov["BTC/USD"]["gap_ranges"] == ["2026-11-08"]  # a weekend day is a crypto gap
    assert cov["BTC/USD"]["expected"] == 12 and not cov["BTC/USD"]["late_start"]
    assert events(conn)[-1][1]["flagged"] == ["AAPL", "BTC/USD"]


def test_run_reports_late_start(conn):
    sessions = weekdays(d("2026-11-02"), d("2026-11-13"))
    eq = [bar(datetime(x.year, x.month, x.day, 5, tzinfo=UTC)) for x in sessions if x >= d("2026-11-09")]
    cl = clients(raw={"AAPL": eq}, crypto={}, sessions=sessions)
    ingest.run(conn, cl, data_checks=False, now=lambda: AFTER_ALL, sleep=lambda s: None, equity_symbols=["AAPL"],
               crypto_symbols=(), start_date=d("2026-11-02"))
    cov = events(conn)[-1][1]["coverage"]["AAPL"]
    assert cov["late_start"] and cov["first"] == "2026-11-09" and cov["gap_ranges"] == []


def test_format_report_lists_flagged_symbols():
    exp = ["2026-11-02", "2026-11-03", "2026-11-04"]
    rep = {"AAPL": {"asset_class": "us_equity", **ingest.symbol_coverage(exp, exp)},
           "NEM": {"asset_class": "us_equity", **ingest.symbol_coverage(["2026-11-03"], exp)}}
    text = ingest.format_report(rep, d("2026-11-02"))
    assert "NEM: starts 2026-11-03" in text and "NEM: 1 missing days: 2026-11-04" in text
    assert "AAPL:" not in text


# --- CLI -------------------------------------------------------------------

def test_cli_start_and_days_are_exclusive(capsys):
    with pytest.raises(SystemExit):
        ingest.main(["--start", "2024-11-01", "--days", "5"])


def test_cli_rejects_bad_start_date(capsys):
    with pytest.raises(SystemExit):
        ingest.main(["--start", "2024-13-01"])
    with pytest.raises(SystemExit):
        ingest.main(["--start", "2999-01-01"])


# --- data checks after ingest ----------------------------------------------

def hook_clients(eq_closes=None, skip_eq=()):
    """Mon 9 .. Fri 13 Nov 2026: AAPL every session, BTC every day (the 5-day window to AFTER_ALL)."""
    ds = [date(2026, 11, 9) + timedelta(days=i) for i in range(5)]
    eq_closes = eq_closes or [100.0, 101.0, 100.5, 102.0, 101.5]
    eq = [bar(datetime(x.year, x.month, x.day, 5, tzinfo=UTC), close=c)
          for x, c in zip(ds, eq_closes) if x not in skip_eq]
    cr = [bar(datetime(x.year, x.month, x.day, tzinfo=UTC), close=70000.0 + i) for i, x in enumerate(ds)]
    return clients(raw={"AAPL": eq}, crypto={"BTC/USD": cr})


def hook_run(conn, cl):
    return ingest.run(conn, cl, days=5, now=lambda: AFTER_ALL, sleep=lambda s: None,
                      equity_symbols=["AAPL"], crypto_symbols=("BTC/USD",))


def check_events(conn):
    return [(r["run_id"], r["event"], json.loads(r["detail"])) for r in
            conn.execute("SELECT * FROM heartbeat WHERE job = 'data_checks' ORDER BY heartbeat_id")]


def test_checks_run_after_ingest_with_same_run_id(conn):
    assert hook_run(conn, hook_clients()) == 0
    ingest_run = conn.execute("SELECT run_id FROM heartbeat WHERE job = 'fetch_prices'").fetchone()[0]
    (r1, e1, _), (r2, e2, d) = check_events(conn)
    assert r1 == r2 == ingest_run and (e1, e2, d["status"]) == ("started", "finished", "ok")


def test_check_error_after_ingest_exits_1(conn):
    assert hook_run(conn, hook_clients(skip_eq={date(2026, 11, 11)})) == 1
    _, event, d = check_events(conn)[-1]
    assert event == "error" and [p["check"] for p in d["problems"]] == ["missing_day"]
    # the fetch itself still finished
    assert [r[0] for r in conn.execute("SELECT event FROM heartbeat WHERE job = 'fetch_prices'")] == \
        ["started", "finished"]


def test_check_warning_after_ingest_exits_0(conn):
    assert hook_run(conn, hook_clients(eq_closes=[100.0, 101.0, 131.0, 132.0, 131.5])) == 0  # +30%
    _, event, d = check_events(conn)[-1]
    assert event == "finished" and d["status"] == "warning"
    assert [p["check"] for p in d["problems"]] == ["suspicious_move"]
