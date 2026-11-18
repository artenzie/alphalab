"""Crypto fill bars (protocol §7 rule 4, v1.12): data/fill_prices.py with a fake Alpaca client."""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from data import db, fill_prices as fp  # noqa: E402
from data.ingest import Clients  # noqa: E402
from test_ingest import FakeCrypto, bar  # noqa: E402

UTC = timezone.utc
D = date(2026, 11, 16)
W0 = datetime(2026, 11, 17, 0, 15, tzinfo=UTC)          # window start: 00:15Z on D+1


def clients(data):
    return Clients(trading=None, stock=None, crypto=FakeCrypto(data))


def minutes(*offsets, close=65000.0, sym_close=None):
    return [bar(W0 + timedelta(minutes=m), close=close + m) for m in offsets]


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "t.db")
    yield c
    c.close()


def rows(conn, sym="BTC/USD"):
    return conn.execute("SELECT * FROM crypto_fill_bars WHERE symbol = ? ORDER BY bar_ts, fetched_at",
                        (sym,)).fetchall()


def heartbeats(conn):
    return [(r["event"], json.loads(r["detail"])) for r in
            conn.execute("SELECT * FROM heartbeat WHERE job = ? ORDER BY heartbeat_id", (fp.JOB,))]


def run(conn, data, now, dates=(D,)):
    return fp.run(conn, clients(data), dates, now=lambda: now, sleep=lambda s: None, symbols=sorted(data))


def test_window_is_0015_to_0115_after_the_daily_close():
    assert fp.window(D) == (W0, W0 + timedelta(hours=1))


def test_request_covers_the_window_only(conn):
    c = clients({"BTC/USD": minutes(0)})
    fp.run(conn, c, [D], now=lambda: W0 + timedelta(hours=2), sleep=lambda s: None, symbols=["BTC/USD"])
    req = c.crypto.requests[0]
    start = req.start.replace(tzinfo=UTC) if req.start.tzinfo is None else req.start
    end = req.end.replace(tzinfo=UTC) if req.end.tzinfo is None else req.end
    assert (start, end) == (W0, W0 + timedelta(minutes=59))


def test_stores_only_closed_bars_inside_the_window(conn):
    data = {"BTC/USD": [bar(W0 - timedelta(minutes=1))] + minutes(0, 1, 2)}
    now = W0 + timedelta(minutes=2, seconds=30)            # the 00:17 bar is still open
    assert run(conn, data, now) == 0
    assert [r["bar_ts"] for r in rows(conn)] == ["2026-11-17T00:15:00.000Z", "2026-11-17T00:16:00.000Z"]
    r = rows(conn)[0]
    assert (r["decision_date"], r["window_start"], r["open"], r["feed"]) == (
        "2026-11-16", "2026-11-17T00:15:00.000Z", 64999.0, "crypto")
    fin = heartbeats(conn)[-1]
    assert fin[0] == "finished"
    s = fin[1]["per_symbol"]["BTC/USD"]
    assert (s["inserted"], s["rejected_unclosed"]) == (2, 1)


def test_build_rows_drops_bars_outside_the_window():
    bars = {"BTC/USD": [bar(W0 - timedelta(minutes=1)), bar(W0), bar(W0 + timedelta(hours=1))]}
    stats = {}
    out = fp.build_rows(bars, D, W0 + timedelta(hours=2), stats)
    assert [r["bar_ts"] for r in out] == ["2026-11-17T00:15:00.000Z"]
    assert stats["BTC/USD"]["outside_window"] == 2


def test_rerun_adds_nothing_and_later_run_adds_the_rest(conn):
    data = {"BTC/USD": minutes(0, 1, 2)}
    run(conn, data, W0 + timedelta(minutes=2, seconds=30))
    run(conn, data, W0 + timedelta(minutes=2, seconds=40))
    assert len(rows(conn)) == 2
    run(conn, data, W0 + timedelta(minutes=10))
    assert len(rows(conn)) == 3


def test_a_changed_bar_becomes_a_new_version(conn):
    run(conn, {"BTC/USD": minutes(0)}, W0 + timedelta(minutes=5))
    run(conn, {"BTC/USD": minutes(0, close=65100.0)}, W0 + timedelta(minutes=6))
    rs = rows(conn)
    assert [r["close"] for r in rs] == [65000.0, 65100.0] and rs[0]["fetched_at"] < rs[1]["fetched_at"]


def test_a_bad_bar_fails_the_run_and_stores_nothing(conn):
    data = {"BTC/USD": minutes(0) + [bar(W0 + timedelta(minutes=1), close=65000.0, open=70000.0)]}
    assert run(conn, data, W0 + timedelta(minutes=5)) == 1
    assert rows(conn) == [] and heartbeats(conn)[-1][0] == "error"


def test_refuses_a_window_with_no_closed_minute(conn):
    assert run(conn, {"BTC/USD": minutes(0)}, W0 + timedelta(seconds=59)) == 1
    assert "has closed yet" in heartbeats(conn)[-1][1]["error"]


def test_warns_on_a_late_first_bar_and_a_missing_coin(conn):
    data = {"BTC/USD": minutes(0), "DOGE/USD": minutes(7, close=0.06), "XRP/USD": []}
    assert run(conn, data, W0 + timedelta(minutes=30)) == 0
    w = heartbeats(conn)[-1][1]["warnings"]
    assert any("DOGE/USD" in x and "more than 5 min" in x for x in w)
    assert any("XRP/USD" in x and "no minute bar" in x for x in w)
    assert not any("BTC/USD" in x for x in w)


def test_backfill_runs_one_window_per_date(conn):
    d2 = D + timedelta(days=1)
    w2 = W0 + timedelta(days=1)
    data = {"BTC/USD": minutes(0) + [bar(w2, close=66000.0)]}
    c = clients(data)
    assert fp.run(conn, c, [d2, D], now=lambda: w2 + timedelta(hours=1), sleep=lambda s: None,
                  symbols=["BTC/USD"]) == 0
    assert len(c.crypto.requests) == 2
    assert [r["decision_date"] for r in rows(conn)] == ["2026-11-16", "2026-11-17"]


def test_last_started_date():
    assert fp.last_started_date(W0 + timedelta(minutes=1)) == D
    assert fp.last_started_date(W0 + timedelta(seconds=30)) == D - timedelta(days=1)


def test_caller_run_id(conn):
    data = {"BTC/USD": minutes(0, 1)}
    assert fp.run(conn, clients(data), [D], now=lambda: W0 + timedelta(minutes=5), sleep=lambda s: None,
                  symbols=["BTC/USD"], run_id="runner-2") == 0
    assert {r[0] for r in conn.execute("SELECT run_id FROM heartbeat")} == {"runner-2"}


# --- v1.13: the window starts at the day's crypto decision time (t_ref) -------

T_REF = datetime(2026, 11, 17, 3, 10, tzinfo=UTC)        # a late decision recorded at 03:09:20Z


def windows(conn):
    return conn.execute("SELECT * FROM crypto_fill_windows ORDER BY window_id").fetchall()


def test_window_starts_at_t_ref_when_given():
    assert fp.window(D, T_REF) == (T_REF, T_REF + timedelta(hours=1))
    with pytest.raises(ValueError, match="whole minute"):
        fp.window(D, T_REF + timedelta(seconds=20))
    with pytest.raises(ValueError, match="before"):
        fp.window(D, W0 - timedelta(minutes=1))


def test_late_window_is_fetched_and_stored_from_t_ref(conn):
    data = {"BTC/USD": [bar(T_REF + timedelta(minutes=m), close=65000.0 + m) for m in (-1, 0, 1, 59, 60)]}
    c = clients(data)
    now = T_REF + timedelta(hours=2)
    assert fp.run(conn, c, [D], now=lambda: now, sleep=lambda s: None, symbols=["BTC/USD"],
                  window_starts={D: T_REF}) == 0
    req = c.crypto.requests[0]
    start = req.start.replace(tzinfo=UTC) if req.start.tzinfo is None else req.start
    assert start == T_REF
    got = rows(conn)
    assert [r["bar_ts"] for r in got] == ["2026-11-17T03:10:00.000Z", "2026-11-17T03:11:00.000Z",
                                          "2026-11-17T04:09:00.000Z"]
    assert {r["window_start"] for r in got} == {"2026-11-17T03:10:00.000Z"}
    assert heartbeats(conn)[-1][1]["per_date"][D.isoformat()]["window_start"] == "2026-11-17T03:10:00.000Z"


def test_a_window_is_recorded_only_once_fully_closed(conn):
    data = {"BTC/USD": minutes(0, 1), "XRP/USD": []}
    assert run(conn, data, W0 + timedelta(minutes=30)) == 0
    assert windows(conn) == []                                         # still open: no evidence yet
    assert run(conn, data, W0 + timedelta(hours=1)) == 0
    w = windows(conn)
    assert len(w) == 1 and (w[0]["window_start"], w[0]["window_end"]) == (db.utc_ts(W0),
                                                                          db.utc_ts(W0 + timedelta(hours=1)))
    assert json.loads(w[0]["symbols"]) == ["BTC/USD", "XRP/USD"] and w[0]["run_id"]
    assert heartbeats(conn)[-1][1]["per_date"][D.isoformat()]["complete"] is True
