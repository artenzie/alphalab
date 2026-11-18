"""Calendar tests: trading days, early closes, run times, US/UK clock changes, cache.

Uses a fake trading client that returns real alpaca-py Calendar objects (naive
ET times, as the API gives them), so no network is needed.
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alpaca.trading.models import Calendar  # noqa: E402

from data import calendar as cal  # noqa: E402
from data import db  # noqa: E402

UTC = timezone.utc

# NYSE 2026-2027 (published schedule). Only what the tests need to be realistic.
HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}
EARLY_CLOSES = {date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)}
RANGE = (date(2026, 1, 1), date(2027, 12, 31))


def utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


class FakeTrading:
    """get_calendar like Alpaca's: trading days only, open/close in ET wall time."""

    def __init__(self, closed=(), failures=()):
        self.closed = set(closed)
        self.failures = list(failures)
        self.calls = 0

    def get_calendar(self, req):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        out, d = [], req.start
        while d <= req.end:
            if d.weekday() < 5 and d not in HOLIDAYS and d not in self.closed:
                close = "13:00" if d in EARLY_CLOSES else "16:00"
                out.append(Calendar(date=d.isoformat(), open="09:30", close=close))
            d += timedelta(days=1)
        return out


class Http403(Exception):
    status_code = 403


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "cal.db")
    yield c
    c.close()


@pytest.fixture
def cached(conn):
    """Cache filled for 2026-2027; the client passed to lookups always fails."""
    assert cal.refresh(conn, FakeTrading(), *RANGE) is not None
    return conn


@pytest.fixture
def offline():
    return FakeTrading(failures=[Http403("network down")] * 10)


# --- trading days -------------------------------------------------------------

def test_normal_day(cached, offline):
    d = date(2026, 11, 18)
    assert cal.is_trading_day(d, cached, offline)
    t = cal.equity_times(d, cached, offline)
    assert t.close == utc(2026, 11, 18, 21)                 # 16:00 EST
    assert t.bars_fetch_at == utc(2026, 11, 18, 22, 30)
    assert t.snapshot_at == utc(2026, 11, 18, 22, 45)
    assert t.decide_at == utc(2026, 11, 18, 23)
    assert offline.calls == 0                               # answered from the cache


@pytest.mark.parametrize("d", [date(2026, 11, 21), date(2026, 11, 22)])
def test_weekend(cached, offline, d):
    assert not cal.is_trading_day(d, cached, offline)
    with pytest.raises(cal.NotATradingDay):
        cal.session_close(d, cached, offline)
    with pytest.raises(cal.NotATradingDay):
        cal.equity_times(d, cached, offline)


@pytest.mark.parametrize("d", [date(2026, 12, 25), date(2026, 11, 26), date(2027, 3, 26)])
def test_holiday(cached, offline, d):
    assert not cal.is_trading_day(d, cached, offline)
    with pytest.raises(cal.NotATradingDay):
        cal.session_close(d, cached, offline)


def test_early_close_day_after_thanksgiving(cached, offline):
    d = date(2026, 11, 27)
    assert cal.session_close(d, cached, offline) == utc(2026, 11, 27, 18)   # 13:00 EST
    t = cal.equity_times(d, cached, offline)
    assert (t.bars_fetch_at, t.snapshot_at, t.decide_at) == (
        utc(2026, 11, 27, 19, 30), utc(2026, 11, 27, 19, 45), utc(2026, 11, 27, 20))
    assert cal.to_et(t.decide_at).strftime("%H:%M") == "15:00"
    assert cal.to_uk(t.decide_at).strftime("%H:%M") == "20:00"


def test_christmas_eve_early_close(cached, offline):
    assert cal.session_close(date(2026, 12, 24), cached, offline) == utc(2026, 12, 24, 18)


def test_all_times_are_utc(cached, offline):
    for v in cal.equity_times(date(2026, 11, 18), cached, offline).as_dict().values():
        assert v.tzinfo is UTC
    for v in cal.crypto_times(date(2026, 11, 18)).as_dict().values():
        assert v.tzinfo is UTC


# --- US / UK clock changes ----------------------------------------------------
# 2026: UK -> GMT Sun 25 Oct, US -> EST Sun 1 Nov.
# 2027: US -> EDT Sun 14 Mar, UK -> BST Sun 28 Mar.

@pytest.mark.parametrize("d, close_utc, et, uk", [
    # both on standard time
    (date(2026, 11, 18), 21, "18:00 EST", "23:00 GMT"),
    (date(2027, 1, 12), 21, "18:00 EST", "23:00 GMT"),
    # both on summer time: same UK wall time as winter
    (date(2026, 7, 15), 20, "18:00 EDT", "23:00 BST"),
    (date(2026, 10, 23), 20, "18:00 EDT", "23:00 BST"),   # Fri before UK change
    # only the UK has changed (one week): decision at 22:00 UK
    (date(2026, 10, 26), 20, "18:00 EDT", "22:00 GMT"),
    (date(2026, 10, 30), 20, "18:00 EDT", "22:00 GMT"),
    # US catches up
    (date(2026, 11, 2), 21, "18:00 EST", "23:00 GMT"),
    # only the US has changed (two weeks in 2027): decision at 22:00 UK
    (date(2027, 3, 12), 21, "18:00 EST", "23:00 GMT"),    # Fri before US change
    (date(2027, 3, 15), 20, "18:00 EDT", "22:00 GMT"),
    (date(2027, 3, 16), 20, "18:00 EDT", "22:00 GMT"),
    (date(2027, 3, 25), 20, "18:00 EDT", "22:00 GMT"),
    # UK catches up
    (date(2027, 3, 29), 20, "18:00 EDT", "23:00 BST"),
])
def test_decide_time_across_clock_changes(cached, offline, d, close_utc, et, uk):
    t = cal.equity_times(d, cached, offline)
    assert t.close == datetime(d.year, d.month, d.day, close_utc, tzinfo=UTC)
    assert t.decide_at == t.close + timedelta(hours=2)
    assert cal.to_et(t.decide_at).strftime("%H:%M %Z") == et
    assert cal.to_uk(t.decide_at).strftime("%H:%M %Z") == uk


def test_et_conversion_uses_the_date_not_a_fixed_offset():
    assert cal.et_to_utc(date(2026, 10, 30), datetime.min.time().replace(hour=16)) == utc(2026, 10, 30, 20)
    assert cal.et_to_utc(date(2026, 11, 2), datetime.min.time().replace(hour=16)) == utc(2026, 11, 2, 21)


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        cal.to_uk(datetime(2026, 11, 18, 23))


# --- crypto -------------------------------------------------------------------

def test_crypto_times():
    t = cal.crypto_times(date(2026, 11, 27))
    assert t.close == utc(2026, 11, 28, 0)
    assert (t.bars_fetch_at, t.snapshot_at, t.decide_at) == (
        utc(2026, 11, 28, 0, 5), utc(2026, 11, 28, 0, 10), utc(2026, 11, 28, 0, 15))


@pytest.mark.parametrize("d", [date(2026, 10, 24), date(2026, 10, 25), date(2026, 11, 1),
                               date(2026, 11, 21), date(2026, 12, 25), date(2027, 3, 14),
                               date(2027, 3, 28)])
def test_crypto_runs_every_day_and_ignores_clock_changes(d):
    nxt = d + timedelta(days=1)
    assert cal.crypto_times(d).decide_at == datetime(nxt.year, nxt.month, nxt.day, 0, 15, tzinfo=UTC)


def test_crypto_year_end():
    assert cal.crypto_times(date(2026, 12, 31)).decide_at == utc(2027, 1, 1, 0, 15)


# --- cache --------------------------------------------------------------------

def counts(conn):
    return tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in ("calendar_fetches", "market_sessions"))


def test_cache_stores_utc_rows(cached):
    row = cached.execute("SELECT open_ts, close_ts FROM market_sessions "
                         "WHERE session_date = '2026-11-27'").fetchone()
    assert tuple(row) == ("2026-11-27T14:30:00.000Z", "2026-11-27T18:00:00.000Z")


def test_refresh_is_idempotent(cached):
    before = counts(cached)
    assert cal.refresh(cached, FakeTrading(), *RANGE) is None
    assert counts(cached) == before


def test_later_closure_is_a_new_fetch_not_an_edit(cached, offline):
    d = date(2026, 11, 18)
    before = counts(cached)
    assert cal.refresh(cached, FakeTrading(closed={d}), *RANGE) is not None
    assert not cal.is_trading_day(d, cached, offline)
    assert cal.is_trading_day(date(2026, 11, 19), cached, offline)
    after = counts(cached)
    assert after[0] == before[0] + 1 and after[1] > before[1]       # old rows kept


def test_narrower_newer_fetch_only_overrides_its_range(cached, offline):
    d = date(2026, 11, 18)
    cal.refresh(cached, FakeTrading(closed={d}), date(2026, 11, 16), date(2026, 11, 20))
    assert not cal.is_trading_day(d, cached, offline)
    assert cal.is_trading_day(date(2026, 11, 23), cached, offline)   # from the wide fetch


def test_network_blip_after_caching_doesnt_matter(cached, offline):
    assert cal.is_trading_day(date(2027, 3, 16), cached, offline)
    assert offline.calls == 0


def test_cache_miss_fetches_once(conn):
    fake = FakeTrading()
    assert cal.is_trading_day(date(2026, 11, 18), conn, fake)
    assert cal.is_trading_day(date(2026, 11, 19), conn, fake)
    assert fake.calls == 1


def test_cache_miss_while_offline_raises(conn, offline):
    with pytest.raises(cal.CalendarUnavailable):
        cal.is_trading_day(date(2026, 11, 18), conn, offline)
    assert counts(conn) == (0, 0)


def test_refresh_retries_transient_errors(conn):
    fake = FakeTrading(failures=[requests.exceptions.ConnectionError("blip")])
    waits = []
    assert cal.refresh(conn, fake, *RANGE, sleep=waits.append) is not None
    assert fake.calls == 2 and len(waits) == 1


def test_alpaca_time_handles_naive_et():
    c = Calendar(date="2026-07-02", open="09:30", close="16:00")
    assert c.close.tzinfo is None                                     # as alpaca-py gives it
    assert cal._alpaca_time(date(2026, 7, 2), c.close) == utc(2026, 7, 2, 20)
    assert cal._alpaca_time(date(2026, 7, 2), "13:00") == utc(2026, 7, 2, 17)


# --- late-run deadline (protocol v1.13) -----------------------------------------

def test_equity_deadline_is_an_hour_before_the_next_open(cached, offline):
    # Wed 18 Nov -> Thu 19 Nov open 14:30Z
    assert cal.decision_deadline("us_equity", date(2026, 11, 18), cached, offline) == utc(2026, 11, 19, 13, 30)
    # Fri 20 Nov -> Mon 23 Nov; Wed 25 Nov -> Fri 27 Nov (Thanksgiving closed)
    assert cal.decision_deadline("us_equity", date(2026, 11, 20), cached, offline) == utc(2026, 11, 23, 13, 30)
    assert cal.decision_deadline("us_equity", date(2026, 11, 25), cached, offline) == utc(2026, 11, 27, 13, 30)


def test_crypto_deadline_is_an_hour_before_the_next_decision():
    assert cal.decision_deadline("crypto", date(2026, 11, 18)) == utc(2026, 11, 19, 23, 15)
