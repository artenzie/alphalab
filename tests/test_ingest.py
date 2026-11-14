"""Ingestion tests with fake Alpaca clients (no network)."""

import json
import sys
from datetime import datetime, timezone
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


class FakeTrading:
    def __init__(self, untradable=()):
        self.untradable = set(untradable)

    def get_asset(self, sym):
        return SimpleNamespace(symbol=sym, tradable=sym not in self.untradable, status="AssetStatus.ACTIVE")


class FakeStock:
    def __init__(self, raw, split, failures=()):
        self.data = {"raw": raw, "split": split}
        self.failures = list(failures)
        self.calls = 0

    def get_stock_bars(self, req):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return SimpleNamespace(data=self.data[req.adjustment.value])


class FakeCrypto:
    def __init__(self, data):
        self.data = data

    def get_crypto_bars(self, req):
        return SimpleNamespace(data=self.data)


def clients(raw=None, split=None, crypto=None, failures=(), untradable=()):
    raw = {"AAPL": [bar(EQ_TS)]} if raw is None else raw
    split = raw if split is None else split
    crypto = {"BTC/USD": [bar(CR_TS, close=70000.0)]} if crypto is None else crypto
    return ingest.Clients(FakeTrading(untradable), FakeStock(raw, split, failures), FakeCrypto(crypto))


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "test.db")
    yield c
    c.close()


def do_run(conn, cl, now=AFTER_ALL, sleeps=None):
    sleeps = [] if sleeps is None else sleeps
    return ingest.run(conn, cl, days=5, now=lambda: now, sleep=sleeps.append,
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
        assert ingest.run(conn, clients(), days=5, now=clock, sleep=lambda s: None,
                          equity_symbols=["AAPL"], crypto_symbols=("BTC/USD",)) == 0
    assert len(prices(conn)) == 2
    finished = [d for e, d in events(conn) if e == "finished"]
    assert [f["totals"]["inserted"] for f in finished] == [2, 0, 0]
    assert finished[-1]["totals"]["unchanged"] == 2


def test_changed_values_create_new_version(conn):
    clock = Clock(AFTER_ALL)
    kw = dict(days=5, now=clock, sleep=lambda s: None, equity_symbols=["AAPL"], crypto_symbols=("BTC/USD",))
    raw = {"AAPL": [bar(EQ_TS, close=400.0)]}
    ingest.run(conn, clients(raw, raw), **kw)
    first_fetch = prices(conn)[0]["fetched_at"]
    # A 4:1 split is applied later: raw unchanged, adjusted values change.
    ingest.run(conn, clients(raw, {"AAPL": [bar(EQ_TS, close=100.0)]}), **kw)
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
