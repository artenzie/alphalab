"""Snapshot tests: the snapshot can't see tomorrow (CLAUDE.md rule 1, protocol §4).

No network. The calendar cache is seeded by hand: weekdays 2025-2027, closed on
Thanksgiving (Thu 26 Nov 2026) and Christmas 2026, early close (13:00 ET) on
Fri 27 Nov and Thu 24 Dec 2026. Bars are stored as ingest would store them:
equities fetched at close + 1:30, crypto at 00:05Z the next day, and every 17th
bar re-versioned (a small correction) three days later.
"""

import json
import math
import random
import sqlite3
import statistics
import sys
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data import calendar as cal  # noqa: E402
from data import db  # noqa: E402
from data import snapshot as sn  # noqa: E402
from data.snapshot import Bar, SnapshotError  # noqa: E402

UTC = timezone.utc
HOLIDAYS = {date(2026, 11, 26), date(2026, 12, 25)}
EARLY = {date(2026, 11, 27), date(2026, 12, 24)}
EQ_SYMS = ["AAPL", "SPY"]
CR_SYMS = ["BTC/USD"]
EQ_START, CR_START = date(2025, 9, 1), date(2025, 10, 1)
LATER = datetime(2027, 6, 1, tzinfo=UTC)   # "now" for backtests: after every test date
REAL_DB = Path(__file__).resolve().parents[1] / "data" / "alphalab.db"


def days(a, b):
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def sessions(a, b):
    return [d for d in days(a, b) if d.weekday() < 5 and d not in HOLIDAYS]


def close_of(d):
    return cal.et_to_utc(d, dtime(13 if d in EARLY else 16))


def seed_calendar(conn, a=date(2025, 1, 1), b=date(2027, 12, 31)):
    with conn:
        fid = conn.execute("INSERT INTO calendar_fetches (range_start, range_end, n_sessions) VALUES (?, ?, ?)",
                           (a.isoformat(), b.isoformat(), len(sessions(a, b)))).lastrowid
        conn.executemany(
            "INSERT INTO market_sessions (fetch_id, session_date, open_ts, close_ts) VALUES (?, ?, ?, ?)",
            [(fid, d.isoformat(), db.utc_ts(cal.et_to_utc(d, dtime(9, 30))), db.utc_ts(close_of(d)))
             for d in sessions(a, b)])


def add(conn, sym, d, close, volume=1_000_000.0, fetched_at=None, ratio=1.0, bar_ts=None):
    """One bar; adjusted = raw * ratio. Default fetch time: the protocol's fetch step."""
    crypto = "/" in sym
    raw = dict(open=close * 0.995, high=close * 1.01, low=close * 0.99, close=close, volume=volume)
    adj = {f"adj_{k}": raw[k] * ratio for k in ("open", "high", "low", "close")}
    adj["adj_volume"] = volume / ratio
    if bar_ts is None:
        bar_ts = datetime(d.year, d.month, d.day, tzinfo=UTC) if crypto else cal.et_to_utc(d, dtime(0))
    if fetched_at is None:
        fetched_at = (cal.crypto_times(d).bars_fetch_at if crypto
                      else close_of(d) + cal.BARS_FETCH_AFTER_CLOSE)
    row = {"symbol": sym, "asset_class": "crypto" if crypto else "us_equity", "bar_date": d.isoformat(),
           "bar_ts": db.utc_ts(bar_ts), "feed": "crypto" if crypto else "iex",
           "fetched_at": fetched_at if isinstance(fetched_at, str) else db.utc_ts(fetched_at), **raw, **adj}
    with conn:
        conn.execute(f"INSERT INTO prices ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                     list(row.values()))


def price(sym, i):
    base = {"AAPL": 200.0, "SPY": 600.0, "BTC/USD": 90000.0}[sym]
    return base * (1 + 0.02 * math.sin(i / 5 + len(sym)) + 0.0005 * i)


def seed_prices(conn, eq_end=date(2026, 11, 27), cr_end=date(2026, 11, 27), reverse=False):
    rows = []
    for sym in EQ_SYMS:
        for i, d in enumerate(sessions(EQ_START, eq_end)):
            rows.append((sym, d, i))
    for sym in CR_SYMS:
        for i, d in enumerate(days(CR_START, cr_end)):
            rows.append((sym, d, i))
    for sym, d, i in (reversed(rows) if reverse else rows):
        add(conn, sym, d, price(sym, i), volume=1e6 + 1000 * (i % 13))
        if i % 17 == 5:  # a later correction: new version three days after the first fetch
            first = (cal.crypto_times(d).bars_fetch_at if "/" in sym
                     else close_of(d) + cal.BARS_FETCH_AFTER_CLOSE)
            add(conn, sym, d, price(sym, i) * 1.001, volume=1e6 + 1000 * (i % 13),
                fetched_at=first + timedelta(days=3))


def make_db(path, **kw):
    c = db.init_db(path)
    seed_calendar(c)
    seed_prices(c, **kw)
    return c


@pytest.fixture
def conn(tmp_path):
    c = make_db(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def conn19(tmp_path):
    """Data up to Thu 19 Nov 2026 only."""
    c = make_db(tmp_path / "t19.db", eq_end=date(2026, 11, 19), cr_end=date(2026, 11, 19))
    yield c
    c.close()


def build(conn, ac, d, mode, **kw):
    kw.setdefault("now", LATER)
    kw.setdefault("symbols", EQ_SYMS if ac == "us_equity" else CR_SYMS)
    return sn.build_snapshot(conn, ac, d, mode, **kw)


def cutoff(conn, ac, d):
    return sn.cutoff_for(conn, ac, d)


THU = date(2026, 11, 19)
FRI = date(2026, 11, 20)


# ---------------------------------------------------------------------------
# 1. A bar dated tomorrow, with an absurd price, changes nothing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", sn.MODES)
def test_tomorrows_bar_changes_nothing(conn19, mode):
    before = {ac: build(conn19, ac, THU, mode) for ac in sn.ASSET_CLASSES}
    eq_cut, cr_cut = cutoff(conn19, "us_equity", THU), cutoff(conn19, "crypto", THU)
    # Fri 20 Nov bars at 1e9, one version fetched *before* Thursday's cutoff (impossible
    # in practice, but the guard mustn't rely on that) and one after.
    for sym in EQ_SYMS:
        add(conn19, sym, FRI, 1e9, fetched_at=eq_cut - timedelta(minutes=10))
        add(conn19, sym, FRI, 1e9, fetched_at=eq_cut + timedelta(days=1))
    # Crypto bar of 20 Nov starts at 00:00Z 20 Nov, i.e. before Thursday's cutoff
    # (00:10Z 20 Nov), and is fetched before it too: only bar_date and the
    # closed-bar rule keep it out.
    add(conn19, "BTC/USD", FRI, 1e9, fetched_at=cr_cut - timedelta(minutes=5))
    add(conn19, "BTC/USD", FRI, 1e9, fetched_at=cr_cut + timedelta(days=1))
    for ac in sn.ASSET_CLASSES:
        after = build(conn19, ac, THU, mode)
        assert after.sha256 == before[ac].sha256
        assert after.canonical == before[ac].canonical
        for sym in after.symbols:
            assert after.history(sym)[-1].date == THU.isoformat()
            assert all(b.close < 1e6 for b in after.history(sym))


# ---------------------------------------------------------------------------
# 2. A version fetched after the cutoff is ignored in live mode
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ac,sym", [("us_equity", "AAPL"), ("crypto", "BTC/USD")])
def test_late_version_ignored_live(conn19, ac, sym):
    live, back = build(conn19, ac, THU, "live"), build(conn19, ac, THU, "backtest")
    cut = cutoff(conn19, ac, THU)
    add(conn19, sym, THU, 12345.0, fetched_at=cut + timedelta(milliseconds=1))
    assert build(conn19, ac, THU, "live").sha256 == live.sha256
    # backtest takes the latest version whatever its fetch time: that's its limitation
    b2 = build(conn19, ac, THU, "backtest")
    assert b2.sha256 != back.sha256
    assert b2.payload["symbols"][sym]["last_close"] == 12345.0
    # a version fetched just before the cutoff is used
    add(conn19, sym, THU, 54321.0, fetched_at=cut - timedelta(milliseconds=1))
    assert build(conn19, ac, THU, "live").payload["symbols"][sym]["last_close"] == 54321.0


# ---------------------------------------------------------------------------
# 3. Unclosed bars are ignored
# ---------------------------------------------------------------------------

EARLY_FRI = date(2026, 11, 27)


def test_early_close_day_bar_accepted_at_protocol_cutoff(conn):
    """27 Nov closes 13:00 ET; the snapshot at 14:45 ET uses that day's bar."""
    snap = build(conn, "us_equity", EARLY_FRI, "live")
    assert snap.cutoff_ts == "2026-11-27T19:45:00.000Z"  # 13:00 ET (18:00Z) + 1:45
    assert snap.payload["symbols"]["AAPL"]["last_date"] == "2026-11-27"


def _load(conn, ac, d, cut, mode, syms):
    co = (lambda x: close_of(x)) if ac == "us_equity" else None
    return sn.load_bars(conn, ac, syms, d, cut, mode, co)


def test_equity_unclosed_bar_ignored_early_close(conn):
    just_before = datetime(2026, 11, 27, 17, 59, 59, 999000, tzinfo=UTC)  # 12:59:59.999 ET
    at_close = datetime(2026, 11, 27, 18, 0, tzinfo=UTC)                   # 13:00 ET
    # backtest mode so that only the closed-bar rule (not fetched_at) decides
    assert _load(conn, "us_equity", EARLY_FRI, just_before, "backtest", ["AAPL"])["AAPL"][-1].date == "2026-11-25"
    assert _load(conn, "us_equity", EARLY_FRI, at_close, "backtest", ["AAPL"])["AAPL"][-1].date == "2026-11-27"


def test_equity_unclosed_bar_ignored_normal_day(conn19):
    """A 20 Nov bar fetched mid-session is rejected until 16:00 ET, even in live mode."""
    add(conn19, "AAPL", FRI, 999.0, fetched_at=datetime(2026, 11, 20, 18, 0, tzinfo=UTC))  # 13:00 ET
    mid = datetime(2026, 11, 20, 20, 59, 59, 999000, tzinfo=UTC)   # 15:59:59.999 ET
    for mode in sn.MODES:
        assert _load(conn19, "us_equity", FRI, mid, mode, ["AAPL"])["AAPL"][-1].date == "2026-11-19"
    closed = datetime(2026, 11, 20, 21, 0, tzinfo=UTC)
    assert _load(conn19, "us_equity", FRI, closed, "live", ["AAPL"])["AAPL"][-1].close == pytest.approx(999.0)


def test_crypto_unclosed_bar_ignored(conn19):
    """The 20 Nov crypto bar (00:00Z 20 Nov - 00:00Z 21 Nov), fetched at 00:05Z 20 Nov."""
    add(conn19, "BTC/USD", FRI, 1e9, fetched_at=datetime(2026, 11, 20, 0, 5, tzinfo=UTC))
    cut = datetime(2026, 11, 20, 0, 10, tzinfo=UTC)
    # decision date 20 Nov, so the bar_date rule lets it through: only the 24h rule stops it
    for mode in sn.MODES:
        bars = _load(conn19, "crypto", FRI, cut, mode, ["BTC/USD"])["BTC/USD"]
        assert bars[-1].date == "2026-11-19"
    just_before = datetime(2026, 11, 20, 23, 59, 59, 999000, tzinfo=UTC)
    assert _load(conn19, "crypto", FRI, just_before, "live", ["BTC/USD"])["BTC/USD"][-1].date == "2026-11-19"
    at = datetime(2026, 11, 21, 0, 0, tzinfo=UTC)
    assert _load(conn19, "crypto", FRI, at, "live", ["BTC/USD"])["BTC/USD"][-1].date == "2026-11-20"


def test_bar_dated_after_decision_date_ignored_even_if_closed(conn):
    """bar_date <= D is a guard of its own: a closed later bar is still out."""
    late = datetime(2026, 11, 28, tzinfo=UTC)
    for ac, sym in (("us_equity", "AAPL"), ("crypto", "BTC/USD")):
        bars = _load(conn, ac, THU, late, "backtest", [sym])[sym]
        assert bars[-1].date == "2026-11-19"


# ---------------------------------------------------------------------------
# 4. Replay: the full database gives the same snapshot as one cut at D
# ---------------------------------------------------------------------------

def _copy(src: sqlite3.Connection) -> sqlite3.Connection:
    dst = sqlite3.connect(":memory:")
    src.backup(dst)
    dst.row_factory = sqlite3.Row
    dst.execute("DROP TRIGGER IF EXISTS prices_no_delete")  # the copy only; the original stays append-only
    return dst


def _truncated(src, ac, d, mode):
    dst = _copy(src)
    cut = db.utc_ts(sn.cutoff_for(src, ac, d))
    with dst:
        dst.execute("DELETE FROM prices WHERE bar_date > ?", (d.isoformat(),))
        if mode == "live":
            dst.execute("DELETE FROM prices WHERE fetched_at > ?", (cut,))
    return dst


def _random_dates(ac, last, n=20, seed=20261114):
    pool = (sessions(date(2026, 1, 5), last) if ac == "us_equity" else days(date(2026, 1, 5), last))
    return sorted(random.Random(seed).sample(pool, n))


@pytest.mark.parametrize("mode", sn.MODES)
@pytest.mark.parametrize("ac", sn.ASSET_CLASSES)
def test_replay_synthetic(conn, ac, mode):
    for d in _random_dates(ac, date(2026, 11, 24)):
        full = build(conn, ac, d, mode)
        cut_db = _truncated(conn, ac, d, mode)
        try:
            assert build(cut_db, ac, d, mode).sha256 == full.sha256, d
        finally:
            cut_db.close()
        assert full.payload["symbols"][full.symbols[0]]["last_date"] == d.isoformat()


@pytest.mark.skipif(not REAL_DB.exists(), reason="no real database")
@pytest.mark.parametrize("ac", sn.ASSET_CLASSES)
def test_replay_real_db_backtest(ac):
    src = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    real = _copy(src)  # work on a copy; the real file is only read
    src.close()
    try:
        sym = "SPY" if ac == "us_equity" else "BTC/USD"
        dates = [date.fromisoformat(r[0]) for r in real.execute(
            "SELECT DISTINCT bar_date FROM prices WHERE symbol = ? AND bar_date >= '2025-01-01' "
            "AND bar_date < '2026-11-13' ORDER BY bar_date", (sym,))]
        now = datetime.now(UTC)
        for d in sorted(random.Random(20261114).sample(dates, 20)):
            full = sn.build_snapshot(real, ac, d, "backtest", now=now)
            cut_db = _truncated(real, ac, d, "backtest")
            try:
                assert sn.build_snapshot(cut_db, ac, d, "backtest", now=now).sha256 == full.sha256, d
            finally:
                cut_db.close()
            assert full.payload["symbols"][sym]["last_date"] == d.isoformat()
    finally:
        real.close()


# ---------------------------------------------------------------------------
# 5. Indicators match hand calculations
# ---------------------------------------------------------------------------

def bars_from(closes, volumes=None):
    volumes = volumes or [1000.0] * len(closes)
    d0 = date(2026, 1, 1)
    return [Bar((d0 + timedelta(days=i)).isoformat(), c, c, c, c, v)
            for i, (c, v) in enumerate(zip(closes, volumes))]


def test_indicators_linear_series():
    # closes 100, 101, ..., 200 (101 bars); volume 1000 except 3000 on the last day
    closes = [100.0 + i for i in range(101)]
    vols = [1000.0] * 100 + [3000.0]
    f = sn.compute_fields(bars_from(closes, vols), "us_equity")
    assert f["n_bars"] == 101
    assert f["last_close"] == 200.0
    assert f["closes_20"] == [181.0 + i for i in range(20)]
    assert f["dates_20"][-1] == "2026-04-11"   # 1 Jan + 100 days
    assert f["ret_1"] == pytest.approx(200 / 199 - 1)
    assert f["ret_5"] == pytest.approx(200 / 195 - 1)
    assert f["ret_21"] == pytest.approx(200 / 179 - 1)
    assert f["ret_63"] == pytest.approx(200 / 137 - 1)
    assert f["ma_20"] == pytest.approx(190.5)    # mean of 181..200
    assert f["ma_100"] == pytest.approx(150.5)   # mean of 101..200
    # 20 consecutive integers: sample variance = 20 * 21 / 12 = 35
    assert f["z_20"] == pytest.approx((200 - 190.5) / math.sqrt(35))
    assert f["volume_ratio_20"] == pytest.approx(3000 / ((19 * 1000 + 3000) / 20))  # = 2.7272...


def test_volatility_alternating_returns():
    # daily returns +10%, -10%, +10%, ... (20 of them): mean 0,
    # sample variance = 20 * 0.01 / 19
    closes = [100.0]
    for i in range(20):
        closes.append(closes[-1] * (1.1 if i % 2 == 0 else 0.9))
    sd = math.sqrt(20 * 0.01 / 19)
    assert sn.compute_fields(bars_from(closes), "us_equity")["vol_20"] == pytest.approx(sd * math.sqrt(252))
    assert sn.compute_fields(bars_from(closes), "crypto")["vol_20"] == pytest.approx(sd * math.sqrt(365))
    # cross-check with the standard library
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, 21)]
    assert sd == pytest.approx(statistics.stdev(rets))


def test_short_history_gives_null():
    f = sn.compute_fields(bars_from([10.0, 11.0, 12.0, 13.0, 14.0, 15.0]), "us_equity")
    assert f["ret_1"] == pytest.approx(15 / 14 - 1)
    assert f["ret_5"] == pytest.approx(15 / 10 - 1)
    for k in ("ret_21", "ret_63", "ma_20", "ma_100", "vol_20", "z_20", "volume_ratio_20",
              "closes_20", "dates_20"):
        assert f[k] is None, k
    # exactly 20 bars: 20-day fields exist, but vol_20 needs 21 closes (20 returns)
    f20 = sn.compute_fields(bars_from([float(i) for i in range(1, 21)]), "us_equity")
    assert f20["ma_20"] == pytest.approx(10.5) and f20["vol_20"] is None and f20["ret_21"] is None
    empty = sn.compute_fields([], "us_equity")
    assert empty["n_bars"] == 0 and empty["last_close"] is None


def test_flat_series_z_is_null():
    f = sn.compute_fields(bars_from([50.0] * 30), "us_equity")
    assert f["z_20"] is None and f["vol_20"] == 0.0 and f["ret_21"] == 0.0


def test_uses_split_adjusted_prices(tmp_path):
    c = db.init_db(tmp_path / "split.db")
    seed_calendar(c)
    ds = sessions(date(2026, 10, 1), THU)
    for i, d in enumerate(ds):
        # a 10:1 split on 2 Nov: raw ~1000 before, ~100 after; adjusted ~100 throughout
        pre = d < date(2026, 11, 2)
        add(c, "AAPL", d, (1000.0 if pre else 100.0) * (1 + 0.001 * i), volume=1000.0,
            ratio=0.1 if pre else 1.0)
    snap = build(c, "us_equity", THU, "live", symbols=["AAPL"])
    h = snap.history("AAPL")
    assert h[0].close == pytest.approx(100.0) and h[0].volume == pytest.approx(10000.0)
    assert abs(snap.payload["symbols"]["AAPL"]["ret_21"]) < 0.05   # no fake -90% split crash
    c.close()


# ---------------------------------------------------------------------------
# 6. The hash is deterministic
# ---------------------------------------------------------------------------

def test_hash_deterministic(conn, tmp_path):
    for ac in sn.ASSET_CLASSES:
        a = build(conn, ac, THU, "live")
        b = build(conn, ac, THU, "live", now=datetime(2030, 1, 1, tzinfo=UTC))
        assert a.sha256 == b.sha256 and a.canonical == b.canonical
        assert a.sha256 == sn.sha256(a.canonical)
        assert sn.canonical_json(json.loads(a.canonical)) == a.canonical
        assert len(a.sha256) == 64
    # same data inserted in reverse order
    rev = make_db(tmp_path / "rev.db", reverse=True)
    try:
        for ac in sn.ASSET_CLASSES:
            for mode in sn.MODES:
                assert build(rev, ac, THU, mode).sha256 == build(conn, ac, THU, mode).sha256
    finally:
        rev.close()


def test_hash_changes_with_data(conn19):
    a = build(conn19, "us_equity", THU, "live")
    add(conn19, "SPY", date(2026, 3, 2), 1.0, fetched_at="2026-11-01T00:00:00.000Z")  # an old bar corrected
    b = build(conn19, "us_equity", THU, "live")
    assert a.sha256 != b.sha256
    assert a.payload["symbols"]["SPY"] == b.payload["symbols"]["SPY"]  # 20/100-day fields untouched...
    assert a.history_sha256 != b.history_sha256                       # ...but the history hash catches it


def test_mode_is_in_the_hash(conn):
    live, back = build(conn, "us_equity", THU, "live"), build(conn, "us_equity", THU, "backtest")
    assert live.payload["mode"] == "live" and back.payload["mode"] == "backtest"
    assert live.sha256 != back.sha256


# ---------------------------------------------------------------------------
# History accessor, storing, guards
# ---------------------------------------------------------------------------

def test_history_bounded(conn):
    snap = build(conn, "us_equity", THU, "live")
    h = snap.history("AAPL")
    assert len(h) == 252 and h[-1].date == "2026-11-19"
    assert snap.history("AAPL", 5) == h[-5:]
    assert snap.payload["symbols"]["AAPL"]["n_bars"] == 252
    with pytest.raises(SnapshotError):
        snap.history("AAPL", 253)
    with pytest.raises(SnapshotError):
        snap.history("AAPL", 0)
    with pytest.raises(KeyError):
        snap.history("MSFT")
    assert isinstance(h, tuple)
    with pytest.raises(AttributeError):
        h[-1].close = 1.0  # bars are immutable
    p = snap.payload
    p["symbols"]["AAPL"]["last_close"] = 0
    assert snap.payload["symbols"]["AAPL"]["last_close"] != 0  # payload is a copy


def test_store_live_snapshot(conn19):
    snap = build(conn19, "us_equity", THU, "live")
    now = sn.cutoff_for(conn19, "us_equity", THU) + timedelta(seconds=30)
    sid = sn.store_snapshot(conn19, snap, now=now)
    row = conn19.execute("SELECT * FROM snapshots WHERE snapshot_id = ?", (sid,)).fetchone()
    assert row["sha256"] == snap.sha256 == sn.sha256(row["payload"])
    assert row["cutoff_ts"] == snap.cutoff_ts == "2026-11-19T22:45:00.000Z"
    assert row["built_at"] == db.utc_ts(now)
    assert json.loads(row["checks"]) == [] and row["checks_passed"] == 1
    assert [r["event"] for r in conn19.execute(
        "SELECT event FROM heartbeat WHERE job = 'build_snapshot' ORDER BY heartbeat_id")] == ["started", "finished"]


def test_store_records_failed_checks(conn19):
    snap = build(conn19, "crypto", FRI, "live")  # 20 Nov crypto bar was never stored: stale
    sid = sn.store_snapshot(conn19, snap, now=LATER)
    row = conn19.execute("SELECT checks, checks_passed FROM snapshots WHERE snapshot_id = ?", (sid,)).fetchone()
    assert row["checks_passed"] == 0
    assert "stale" in {p["check"] for p in json.loads(row["checks"])}


def test_store_refuses_backtest_and_early(conn19):
    with pytest.raises(SnapshotError):
        sn.store_snapshot(conn19, build(conn19, "us_equity", THU, "backtest"), now=LATER)
    snap = build(conn19, "us_equity", THU, "live")
    with pytest.raises(SnapshotError):
        sn.store_snapshot(conn19, snap, now=datetime(2026, 11, 19, 22, 44, tzinfo=UTC))
    assert conn19.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0


def test_backtest_refuses_future_cutoff(conn):
    with pytest.raises(SnapshotError):
        build(conn, "us_equity", THU, "backtest", now=datetime(2026, 11, 19, 22, 44, tzinfo=UTC))
    build(conn, "us_equity", THU, "backtest", now=datetime(2026, 11, 19, 22, 45, tzinfo=UTC))


def test_non_trading_day_and_bad_args(conn):
    with pytest.raises(cal.NotATradingDay):
        build(conn, "us_equity", date(2026, 11, 26), "live")   # Thanksgiving
    with pytest.raises(cal.NotATradingDay):
        build(conn, "us_equity", date(2026, 11, 21), "live")   # Saturday
    build(conn, "crypto", date(2026, 11, 21), "live")          # crypto trades every day
    with pytest.raises(SnapshotError):
        build(conn, "us_equity", THU, "replay")
    with pytest.raises(SnapshotError):
        build(conn, "fx", THU, "live")


def test_cutoffs_match_protocol(conn):
    assert db.utc_ts(sn.cutoff_for(conn, "us_equity", THU)) == "2026-11-19T22:45:00.000Z"      # 17:45 ET
    assert db.utc_ts(sn.cutoff_for(conn, "us_equity", EARLY_FRI)) == "2026-11-27T19:45:00.000Z"  # 14:45 ET
    assert db.utc_ts(sn.cutoff_for(conn, "crypto", THU)) == "2026-11-20T00:10:00.000Z"


# ---------------------------------------------------------------------------
# Late runs (protocol v1.13): cutoff = actual build time, labelled late
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ac,sym", [("us_equity", "AAPL"), ("crypto", "BTC/USD")])
def test_late_snapshot_uses_its_build_time_as_cutoff(conn19, ac, sym):
    proto = cutoff(conn19, ac, THU)
    add(conn19, sym, THU, 777.0, fetched_at=proto + timedelta(hours=2))      # fetched late
    on_time = build(conn19, ac, THU, "live")
    assert on_time.payload["late"] is False and on_time.payload["minutes_late"] == 0
    assert on_time.payload["symbols"][sym]["last_close"] != 777.0
    late = build(conn19, ac, THU, "live", cutoff=proto + timedelta(hours=3))
    p = late.payload
    assert p["late"] is True and p["minutes_late"] == 180
    assert p["cutoff_ts"] == db.utc_ts(proto + timedelta(hours=3)) == late.cutoff_ts
    assert p["symbols"][sym]["last_close"] == 777.0
    assert p["symbols"][sym]["last_date"] == THU.isoformat()           # still no next-day bar
    earlier = build(conn19, ac, THU, "live", cutoff=proto + timedelta(hours=1))
    assert earlier.payload["symbols"][sym]["last_close"] != 777.0       # fetched after this cutoff


def test_cutoff_before_the_protocol_cutoff_is_refused(conn19):
    with pytest.raises(SnapshotError, match="before the protocol cutoff"):
        build(conn19, "us_equity", THU, "live", cutoff=cutoff(conn19, "us_equity", THU) - timedelta(minutes=1))


def test_late_snapshot_is_stored_with_its_cutoff(conn19):
    late_cut = cutoff(conn19, "us_equity", THU) + timedelta(minutes=95)
    snap = build(conn19, "us_equity", THU, "live", cutoff=late_cut)
    sid = sn.store_snapshot(conn19, snap, now=late_cut + timedelta(seconds=5), run_id="r1")
    row = conn19.execute("SELECT cutoff_ts, payload FROM snapshots WHERE snapshot_id = ?", (sid,)).fetchone()
    assert row["cutoff_ts"] == db.utc_ts(late_cut)
    hb = conn19.execute("SELECT detail FROM heartbeat WHERE run_id = 'r1' AND event = 'started'").fetchone()
    assert json.loads(hb[0])["late"] is True and json.loads(hb[0])["minutes_late"] == 95
