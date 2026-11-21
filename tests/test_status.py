"""runner/status.py: scheduled runs, outcomes, uptime and tick gaps (D2)."""

import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from data import calendar as cal  # noqa: E402
from data import db  # noqa: E402
from runner import status  # noqa: E402

import test_tick as TT  # noqa: E402
from test_tick import World, activate, make_exp  # noqa: E402

UTC = timezone.utc


def ro(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def outcomes(st):
    return {(d["asset_class"], d["date"]): d["outcome"] for d in st.days}


def test_ok_late_error_missing_pending_and_a_tick_gap(tmp_path):
    path = make_exp(tmp_path)
    activate(tmp_path, "dev")
    w = World(tmp_path, market=TT.CatchUpMarket)
    c = db.connect(path)
    fetch16 = cal.equity_times(date(2026, 11, 16), c, TT.TR.no_network).bars_fetch_at
    c.close()
    w.tick(fetch16)                                             # Mon 16 equity: on time, ok
    w.tick(fetch16 + timedelta(minutes=40))
    # the Pi is off until Wed 18 Nov 13:35Z: Tue 17 equity and crypto 16 are past their deadlines
    now = datetime(2026, 11, 18, 13, 35, tzinfo=UTC)
    st = status.summary(ro(path), now, trading=TT.TR.no_network)
    o = outcomes(st)
    assert o[("us_equity", "2026-11-16")] == "ok"
    assert o[("us_equity", "2026-11-17")] == "missing"
    assert o[("crypto", "2026-11-16")] == "missing"
    assert o[("crypto", "2026-11-17")] == "pending"           # deadline 23:15Z on the 18th
    assert st.scheduled == 3 and abs(st.uptime - 1 / 3) < 1e-12
    assert len(st.gaps) == 1 and st.gaps[0][1] == now            # from the last tick to now
    # back on: the catch-up tick settles the missed days (error: nobody decided) and runs crypto 17 late
    w.tick(datetime(2026, 11, 18, 13, 40, tzinfo=UTC))
    now = datetime(2026, 11, 18, 13, 45, tzinfo=UTC)
    st = status.summary(ro(path), now, trading=TT.TR.no_network)
    o = outcomes(st)
    assert o[("us_equity", "2026-11-17")] == "error" and o[("crypto", "2026-11-16")] == "error"
    assert o[("crypto", "2026-11-17")] == "late"
    c = st.counts()
    assert (c["ok"], c["late"], c["error"], c["missing"], c["pending"]) == (1, 1, 2, 0, 0)
    assert st.uptime == 0.5 and st.on_time == 0.25
    gap = st.gaps[0]                                          # from the end of the last tick on the 16th
    assert len(st.gaps) == 1 and gap[1] == datetime(2026, 11, 18, 13, 40, tzinfo=UTC)
    assert gap[0] >= fetch16 + timedelta(minutes=40)
    assert st.last_runs["crypto"]["date"] == "2026-11-17" and st.last_runs["crypto"]["late"]
    text = status.format_status(st)
    assert "Uptime (D2" in text and "50.0%" in text and "Gaps over 10 min: 1" in text
    assert "us_equity 2026-11-17  error" in text


def test_a_long_tick_is_not_a_gap(tmp_path):
    path = make_exp(tmp_path)
    c = db.init_db(path)
    t = datetime(2026, 11, 16, 21, 30, tzinfo=UTC)
    rows = [(t, t + timedelta(minutes=32)),                    # an on-time equity run: 32 minutes
            (t + timedelta(minutes=35), t + timedelta(minutes=35, seconds=2)),
            (t + timedelta(minutes=40), t + timedelta(minutes=40, seconds=1)),
            (t + timedelta(hours=3), t + timedelta(hours=3, seconds=1))]      # the Pi was off for 2 h 20 m
    with c:
        for a, b in rows:
            c.execute("INSERT INTO heartbeat (run_id, job, event, at, detail) VALUES ('x', 'tick', 'finished', ?, ?)",
                      (db.utc_ts(a), '{"finished_at": "%s"}' % db.utc_ts(b)))
    c.close()
    st = status.summary(ro(path), t + timedelta(hours=3, minutes=2), trading=TT.TR.no_network)
    assert st.gaps == [(t + timedelta(minutes=40, seconds=1), t + timedelta(hours=3))]


def test_main_reads_the_active_experiment(tmp_path, monkeypatch, capsys):
    path = make_exp(tmp_path)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(status.ex, "EXPERIMENTS_DIR", tmp_path)
    assert status.main([]) == 1                                  # nothing active
    activate(tmp_path, "dev --no-ai")
    assert status.main([]) == 0
    out = capsys.readouterr().out
    assert "experiment 'dev'" in out and "Ticks: 0" in out
    assert status.main(["--db", str(path), "--all"]) == 0
