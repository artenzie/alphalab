"""Data-check tests with hand-made bad data (no network).

The calendar cache is seeded by hand: weekdays Oct 2026 - Dec 2027, closed on
Thanksgiving (Thu 26 Nov 2026) and Christmas, early close (13:00 ET) on Fri 27 Nov
and 24 Dec 2026.
"""

import json
import math
import sys
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data import calendar as cal  # noqa: E402
from data import checks, db  # noqa: E402

UTC = timezone.utc
HOLIDAYS = {date(2026, 11, 26), date(2026, 12, 25)}
EARLY = {date(2026, 11, 27), date(2026, 12, 24)}
START = date(2026, 11, 2)                      # Mon
NOW = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)  # Fri 20 Nov, after the 21:00Z close
EQ, CR = "AAPL", "BTC/USD"


def days(a, b):
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def sessions(a, b):
    return [d for d in days(a, b) if d.weekday() < 5 and d not in HOLIDAYS]


def seed_calendar(conn, a=date(2026, 10, 1), b=date(2027, 12, 31)):
    with conn:
        fid = conn.execute("INSERT INTO calendar_fetches (range_start, range_end, n_sessions) VALUES (?, ?, ?)",
                           (a.isoformat(), b.isoformat(), len(sessions(a, b)))).lastrowid
        conn.executemany(
            "INSERT INTO market_sessions (fetch_id, session_date, open_ts, close_ts) VALUES (?, ?, ?, ?)",
            [(fid, d.isoformat(), db.utc_ts(cal.et_to_utc(d, dtime(9, 30))),
              db.utc_ts(cal.et_to_utc(d, dtime(13 if d in EARLY else 16))))
             for d in sessions(a, b)])


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "test.db")
    seed_calendar(c)
    yield c
    c.close()


def add(conn, sym, d, close=100.0, ratio=1.0, fetched_at=None, **kw):
    """One bar; adj = raw * ratio (adj volume = raw / ratio). kw overrides any column."""
    crypto = "/" in sym
    row = dict(open=close * 0.99, high=close * 1.02, low=close * 0.98, close=close, volume=1000.0)
    row.update({f"adj_{k}": row[k] * ratio for k in ("open", "high", "low", "close")})
    row["adj_volume"] = row["volume"] / ratio
    row.update(kw)
    ts = datetime(d.year, d.month, d.day, 0 if crypto else 5, tzinfo=UTC)
    # Default: fetched at the protocol's fetch time (equity 17:30 ET in winter = 22:30Z; crypto 00:05Z next day).
    fetched = fetched_at or db.utc_ts(datetime(d.year, d.month, d.day, tzinfo=UTC)
                                      + (timedelta(days=1, minutes=5) if crypto else timedelta(hours=22, minutes=30)))
    cols = ["symbol", "asset_class", "bar_date", "bar_ts", "feed", "fetched_at", *row]
    vals = [sym, "crypto" if crypto else "us_equity", d.isoformat(), db.utc_ts(ts),
            "crypto" if crypto else "iex", fetched, *row.values()]
    with conn:
        conn.execute(f"INSERT INTO prices ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)


def eq_days(a=START, b=date(2026, 11, 20)):
    return sessions(a, b)


def cr_days(a=START, b=date(2026, 11, 19)):
    return days(a, b)


def fill(conn, eq=None, cr=None, skip=(), cr_skip=()):
    """A clean dataset: gentle drift, every expected day present (minus `skip` for AAPL, `cr_skip` for BTC)."""
    for i, d in enumerate(eq if eq is not None else eq_days()):
        if d not in skip:
            add(conn, EQ, d, close=100 + i * 0.5)
    for i, d in enumerate(cr if cr is not None else cr_days()):
        if d not in cr_skip:
            add(conn, CR, d, close=70000 + i * 100)


def run(conn, now=NOW, start=START, **kw):
    return checks.run_checks(conn, [EQ], [CR], start, now, **kw)


def of(problems, check):
    return [p for p in problems if p["check"] == check]


# --- clean data and output -------------------------------------------------

def test_clean_data_has_no_problems(conn):
    fill(conn)
    assert run(conn) == []


def test_problems_are_json_serialisable_without_nan(conn):
    fill(conn, skip={date(2026, 11, 10)})
    add(conn, EQ, date(2026, 11, 10), high=50.0)
    probs = run(conn)
    assert probs
    json.dumps(probs, allow_nan=False)
    assert set(probs[0]) == {"check", "severity", "symbol", "date", "message", "detail"}


def test_nan_values_become_strings():
    p = checks.problem("bad_bar", "error", EQ, "2026-11-10", "x", value=math.nan, nested=[math.inf])
    assert json.loads(json.dumps(p, allow_nan=False))["detail"] == {"value": "nan", "nested": ["inf"]}


def test_uses_latest_stored_version(conn):
    fill(conn)  # AAPL 10 Nov first stored fine at 22:30Z on 10 Nov, then two newer versions:
    add(conn, EQ, date(2026, 11, 10), close=104, high=1.0, fetched_at="2026-11-11T01:00:00.000Z")  # bad
    add(conn, EQ, date(2026, 11, 10), close=104, fetched_at="2026-11-12T01:00:00.000Z")  # corrected
    assert run(conn) == []
    # ...but a version fetched after `now` is invisible
    assert of(run(conn, now=datetime(2026, 11, 11, 23, tzinfo=UTC)), "bad_bar")


# --- 1. missing days -------------------------------------------------------

def test_missing_equity_session(conn):
    fill(conn, skip={date(2026, 11, 10), date(2026, 11, 11)})
    probs = run(conn)
    eq = [p for p in of(probs, "missing_day") if p["symbol"] == EQ]
    assert len(eq) == 1 and eq[0]["detail"]["ranges"] == ["2026-11-10..2026-11-11"]
    assert eq[0]["severity"] == "error"


def test_weekend_is_not_missing_for_equity_but_is_for_crypto(conn):
    fill(conn, skip={date(2026, 11, 8)}, cr_skip={date(2026, 11, 8)})  # Sunday
    probs = run(conn)
    assert [(p["symbol"], p["detail"]["dates"]) for p in of(probs, "missing_day")] == [(CR, ["2026-11-08"])]


def test_holiday_is_not_missing(conn):
    now = datetime(2026, 11, 30, 23, tzinfo=UTC)
    fill(conn, eq=eq_days(b=date(2026, 11, 30)), cr=cr_days(b=date(2026, 11, 29)))
    assert date(2026, 11, 26) not in eq_days(b=date(2026, 11, 30))
    assert run(conn, now=now) == []


def test_bar_on_a_holiday_is_unexpected(conn):
    now = datetime(2026, 11, 30, 23, tzinfo=UTC)
    fill(conn, eq=eq_days(b=date(2026, 11, 30)), cr=cr_days(b=date(2026, 11, 29)))
    add(conn, EQ, date(2026, 11, 26), close=110)
    (p,) = of(run(conn, now=now), "unexpected_day")
    assert p["symbol"] == EQ and p["detail"]["dates"] == ["2026-11-26"]


def test_no_bars_at_all(conn):
    fill(conn, eq=[])
    probs = [p for p in run(conn) if p["symbol"] == EQ]
    assert {p["check"] for p in probs} == {"missing_day", "stale"}


def test_calendar_unavailable_is_a_problem(tmp_path):
    c = db.init_db(tmp_path / "nocal.db")  # empty calendar cache; the fake can't fetch
    try:
        fill(c)
        probs = run(c, trading=object())
        assert [p["check"] for p in probs] == ["calendar_unavailable"]
        assert checks.has_errors(probs)
    finally:
        c.close()


# --- 2. bad bars -----------------------------------------------------------

@pytest.mark.parametrize("override,text", [
    ({"high": 99.0}, "raw close 100.0 outside"),            # high < close
    ({"low": 99.5}, "raw open 99.0 outside"),               # low > open
    ({"open": 103.0}, "raw open 103.0 outside"),            # open > high
    ({"low": 0.0}, "raw low not a positive number"),
    ({"adj_close": -1.0}, "adjusted close not a positive number"),
    ({"volume": -5.0}, "raw volume -5.0 is negative"),
    ({"adj_volume": -5.0}, "adjusted volume -5.0 is negative"),
])
def test_bad_bar(conn, override, text):
    d = date(2026, 11, 10)
    fill(conn, skip={d})
    add(conn, EQ, d, close=100.0, **override)
    (p,) = of(run(conn), "bad_bar")
    assert p["symbol"] == EQ and p["date"] == "2026-11-10" and p["severity"] == "error"
    assert any(text in f for f in p["detail"]["faults"]), p["detail"]["faults"]


def test_nan_price_is_a_bad_bar():
    b = dict(open=1.0, high=2.0, low=0.5, close=math.nan, volume=1.0,
             adj_open=1.0, adj_high=2.0, adj_low=0.5, adj_close=1.0, adj_volume=1.0)
    assert checks.bar_faults(b) == ["raw close not a positive number"]


def test_bad_crypto_bar(conn):
    d = date(2026, 11, 10)
    fill(conn, cr_skip={d})
    add(conn, CR, d, close=70000, high=60000.0)
    assert [p["symbol"] for p in of(run(conn), "bad_bar")] == [CR]


# --- 3. suspicious moves ---------------------------------------------------

def series(conn, sym, closes, ds, ratios=None):
    for i, (d, c) in enumerate(zip(ds, closes)):
        add(conn, sym, d, close=c, ratio=1.0 if ratios is None else ratios[i])


def test_equity_move_over_25pct_is_a_warning(conn):
    ds = eq_days()
    closes = [100.0] * len(ds)
    closes[7:] = [126.0] * (len(ds) - 7)  # +26% on day 8
    series(conn, EQ, closes, ds)
    fill(conn, eq=[])
    (p,) = run(conn)
    assert p["check"] == "suspicious_move" and p["severity"] == "warning"
    assert p["date"] == ds[7].isoformat() and p["detail"]["change"] == pytest.approx(0.26)
    assert not checks.has_errors([p])


def test_equity_move_under_25pct_and_big_drop(conn):
    ds = eq_days()
    closes = [100.0] * 5 + [124.0] * 5 + [86.0] * (len(ds) - 10)  # +24%, then -30.6%
    series(conn, EQ, closes, ds)
    fill(conn, eq=[])
    assert [p["date"] for p in of(run(conn), "suspicious_move")] == [ds[10].isoformat()]


def test_crypto_thresholds_are_40pct(conn):
    ds = cr_days()
    closes = [100.0] * 5 + [139.0] * 5 + [139.0 * 1.41] * (len(ds) - 10)  # +39% ok, +41% flagged
    series(conn, CR, closes, ds)
    fill(conn, cr=[])
    assert [p["date"] for p in of(run(conn), "suspicious_move")] == [ds[10].isoformat()]


def test_move_from_bar_before_window_is_checked(conn):
    add(conn, EQ, date(2026, 10, 30), close=50.0)  # Fri before the window: +100% into Mon 2 Nov
    fill(conn)
    (p,) = of(run(conn), "suspicious_move")
    assert p["date"] == "2026-11-02" and p["detail"]["prev_date"] == "2026-10-30"


def test_split_adjusted_drop_is_not_a_move(conn):
    ds = eq_days()
    k = 5  # 4:1 split on ds[5]: raw 400 -> 100, adjusted continuous
    series(conn, EQ, [400.0] * k + [101.0] * (len(ds) - k), ds, [0.25] * k + [1.0] * (len(ds) - k))
    fill(conn, eq=[])
    assert run(conn) == []


# --- 4. split consistency --------------------------------------------------

def test_clean_split_is_listed_not_flagged(conn):
    ds = eq_days()
    k = 5
    series(conn, EQ, [400.0] * k + [101.0] * (len(ds) - k), ds, [0.25] * k + [1.0] * (len(ds) - k))
    assert checks.find_splits(conn, [EQ], START, NOW) == [
        {"symbol": EQ, "date": ds[k].isoformat(), "factor": "4:1", "ratio_before": 0.25, "ratio_after": 1.0}]


def test_nvda_like_10_for_1_with_rounding(conn):
    # Real NVDA 3-14 Jun 2024 numbers (raw / Alpaca split-adjusted), shifted into the window.
    raw = [1149.99, 1164.35, 1224.49, 1210.89, 1208.42, 121.93, 120.92, 125.28, 129.37, 131.97]
    adj = [115.000, 116.430, 122.450, 121.090, 120.840, 121.930, 120.920, 125.280, 129.370, 131.970]
    ds = eq_days()[:10]
    for d, r, a in zip(ds, raw, adj):
        add(conn, EQ, d, close=r, ratio=a / r)
    now = datetime(2026, 11, 13, 23, tzinfo=UTC)
    assert [s["factor"] for s in checks.find_splits(conn, [EQ], START, now)] == ["10:1"]
    assert checks.run_checks(conn, [EQ], [], START, now) == []


def test_reverse_split(conn):
    ds = eq_days()
    k = 5  # 1:10 reverse split: raw 2 -> 20
    series(conn, EQ, [2.0] * k + [20.2] * (len(ds) - k), ds, [10.0] * k + [1.0] * (len(ds) - k))
    assert [s["factor"] for s in checks.find_splits(conn, [EQ], START, NOW)] == ["1:10"]
    assert of(checks.run_checks(conn, [EQ], [], START, NOW), "split_inconsistent") == []


def test_bars_adjusted_at_different_times_are_flagged(conn):
    # A 4:1 split on ds[8], but only the last 3 pre-split bars were re-fetched with the new
    # adjustment; older ones still carry ratio 1. The adjusted series jumps -75% on ds[5].
    ds = eq_days()
    ratios = [1.0] * 5 + [0.25] * 3 + [1.0] * (len(ds) - 8)
    series(conn, EQ, [400.0] * 8 + [101.0] * (len(ds) - 8), ds, ratios)
    probs = checks.run_checks(conn, [EQ], [], START, NOW)
    (p,) = of(probs, "split_inconsistent")
    assert p["date"] == ds[5].isoformat() and "wrong day" in p["message"] and p["detail"]["factor"] == 0.25
    assert of(probs, "suspicious_move")


def test_non_split_factor_is_flagged(conn):
    ds = eq_days()
    series(conn, EQ, [100.0] * len(ds), ds, [1 / 1.07] * 5 + [1.0] * (len(ds) - 5))
    (p,) = of(checks.run_checks(conn, [EQ], [], START, NOW), "split_inconsistent")
    assert p["date"] == ds[5].isoformat() and "not a simple split factor" in p["message"]


def test_slow_ratio_drift_is_flagged(conn):
    ds = eq_days()
    series(conn, EQ, [100.0] * len(ds), ds, [1 - 0.0008 * (len(ds) - 1 - i) for i in range(len(ds))])
    probs = of(checks.run_checks(conn, [EQ], [], START, NOW), "split_inconsistent")
    assert probs and all("not a simple split factor" in p["message"] for p in probs)


def test_latest_ratio_not_one_is_flagged(conn):
    ds = eq_days()
    series(conn, EQ, [100.0] * len(ds), ds, [0.5] * len(ds))
    (p,) = of(checks.run_checks(conn, [EQ], [], START, NOW), "split_inconsistent")
    assert p["date"] == ds[-1].isoformat() and "should be 1" in p["message"]


def test_ohlc_ratios_must_agree(conn):
    d = date(2026, 11, 10)
    fill(conn, skip={d})
    add(conn, EQ, d, close=100.0, adj_high=102.0 * 1.01)  # high adjusted by 1.01, the rest by 1
    (p,) = of(run(conn), "split_inconsistent")
    assert p["date"] == "2026-11-10" and "ratios differ" in p["message"]


def test_adjusted_volume_must_match(conn):
    d = date(2026, 11, 10)
    fill(conn, skip={d})
    add(conn, EQ, d, close=100.0, adj_volume=2000.0)
    (p,) = of(run(conn), "split_inconsistent")
    assert "volume" in p["message"]


# --- 5. staleness ----------------------------------------------------------

def test_stale_equity_and_crypto(conn):
    fill(conn, eq=eq_days(b=date(2026, 11, 19)), cr=cr_days(b=date(2026, 11, 18)))
    probs = of(run(conn), "stale")  # the missing last day is also reported as missing_day
    assert {(p["check"], p["symbol"], p["detail"]["expected"]) for p in probs} == {
        ("stale", EQ, "2026-11-20"), ("stale", CR, "2026-11-19")}


def test_equity_not_stale_before_close(conn):
    fill(conn, eq=eq_days(b=date(2026, 11, 19)))
    assert run(conn, now=datetime(2026, 11, 20, 20, 59, tzinfo=UTC)) == []  # 15:59 ET


def test_early_close_session_expected_at_1430_et(conn):
    fill(conn, eq=eq_days(b=date(2026, 11, 25)), cr=cr_days(b=date(2026, 11, 26)))
    at_1430 = datetime(2026, 11, 27, 19, 30, tzinfo=UTC)  # 14:30 ET, early close was 13:00
    (p,) = of(run(conn, now=at_1430), "stale")
    assert p["symbol"] == EQ and p["detail"]["expected"] == "2026-11-27"
    add(conn, EQ, date(2026, 11, 27), close=113.0, fetched_at="2026-11-27T19:30:00.000Z")
    assert run(conn, now=at_1430) == []


def test_bar_for_unclosed_session(conn):
    fill(conn, eq=eq_days(b=date(2026, 11, 19)), cr=cr_days(b=date(2026, 11, 19)))
    add(conn, EQ, date(2026, 11, 20), close=110.0, fetched_at="2026-11-20T19:00:00.000Z")  # wrongly stored
    probs = run(conn, now=datetime(2026, 11, 20, 20, 0, tzinfo=UTC))  # 15:00 ET Fri
    assert {p["check"] for p in probs if p["symbol"] == EQ} == {"unclosed_bar", "unexpected_day"}
    (p,) = of(probs, "unclosed_bar")
    assert p["date"] == "2026-11-20" and p["detail"]["expected"] == "2026-11-19"


# --- alert -----------------------------------------------------------------

def hb(conn):
    return [(r["run_id"], r["job"], r["event"], json.loads(r["detail"]))
            for r in conn.execute("SELECT * FROM heartbeat ORDER BY heartbeat_id")]


def alert(conn):
    return checks.check_and_alert(conn, "run1", [EQ], [CR], START, NOW)


def test_alert_ok(conn):
    fill(conn)
    code, probs, splits = alert(conn)
    assert (code, probs, splits) == (0, [], [])
    (_, _, e1, _), (rid, job, e2, d) = hb(conn)
    assert (rid, job, e1, e2, d["status"]) == ("run1", "data_checks", "started", "finished", "ok")


def test_alert_warning_only_exits_0(conn):
    ds = eq_days()
    series(conn, EQ, [100.0] * 5 + [130.0] * (len(ds) - 5), ds)  # a +30% earnings day
    fill(conn, eq=[])
    code, probs, _ = alert(conn)
    assert code == 0 and len(probs) == 1
    *_, (_, _, event, d) = hb(conn)
    assert event == "finished" and d["status"] == "warning" and d["counts"] == {"error": 0, "warning": 1}
    assert d["problems"] == probs


def test_alert_error_exits_1_with_problem_list(conn):
    fill(conn, skip={date(2026, 11, 10)})
    code, probs, _ = alert(conn)
    assert code == 1
    *_, (rid, job, event, d) = hb(conn)
    assert (rid, job, event, d["status"]) == ("run1", "data_checks", "error", "error")
    assert d["problems"] == probs and probs[0]["check"] == "missing_day"
    assert conn.execute("SELECT json_valid(detail) FROM heartbeat ORDER BY heartbeat_id DESC").fetchone()[0] == 1
