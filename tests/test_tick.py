"""runner/tick.py: what is due, retry cap, settle-only after the deadline, catch-up, lock, --no-ai."""

import fcntl
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from data import calendar as cal  # noqa: E402
from data import db  # noqa: E402
from runner import daily, roster, tick  # noqa: E402
from runner import experiment as ex  # noqa: E402

import test_runner as TR  # noqa: E402

UTC = timezone.utc
START, END = TR.START, date(2026, 11, 24)
REAL_DB = ROOT / "data" / "alphalab.db"


def make_exp(data_dir, name="dev", start=START, end=END):
    c = db.init_db(data_dir / f"{name}.db")
    TR.seed_calendar(c)
    TR.seed_history(c)
    with c:
        c.execute("INSERT INTO experiment (name, start_date, end_date) VALUES (?, ?, ?)",
                  (name, start.isoformat(), end.isoformat()))
    c.close()
    return data_dir / f"{name}.db"


class CatchUpMarket(TR.Market):
    """Like the real ingest (from the last stored bar on): every closed bar not stored yet appears."""

    def ingest(self, conn, asset_class, run_id, now):
        t = now()
        if asset_class == "us_equity":
            days = [d for d in TR.EQ_HIST if TR.close_of(d) <= t]
            syms, price = TR.UNIVERSE, TR.eq_close
        else:
            last = (t - timedelta(minutes=1)).date() - timedelta(days=1)
            days = [last - timedelta(days=i) for i in range(30)]
            syms, price = TR.CRYPTO_SYMBOLS, TR.cr_close
        have = {(r[0], r[1]) for r in conn.execute("SELECT symbol, bar_date FROM prices")}
        TR.insert_rows(conn, [TR.bar_row(s, d, price(s, d), t) for d in days for s in syms
                              if (s, d.isoformat()) not in have])
        self.clock.sleep(60)
        return 0


class World:
    """A fake clock per tick, the fake market and scripted AI clients shared across ticks."""

    def __init__(self, data_dir, market=TR.Market):
        self.data_dir, self.market = data_dir, market
        self.clients = {aid: TR.Client(aid) for aid in roster.EQUITY_AI + roster.CRYPTO_AI}

    def tick(self, at):
        clock = TR.Clock(at)
        return tick.run_tick(self.data_dir, clock, lambda: TR.make_deps(clock, self.market(clock), self.clients),
                             trading=TR.no_network)

    def calls(self, ids=roster.EQUITY_AI):
        return {a: len(self.clients[a].calls) for a in ids}


def activate(data_dir, line):
    (data_dir / tick.ACTIVE_NAME).write_text(line + "\n")


def eq_fetch(d, conn=None):
    c = conn or db.connect(conn_path)
    return cal.equity_times(d, c, TR.no_network).bars_fetch_at


@pytest.fixture
def world(tmp_path):
    global conn_path
    conn_path = make_exp(tmp_path)
    activate(tmp_path, "dev")
    return World(tmp_path)


def started(path, ac, d):
    c = db.connect(path)
    try:
        return len(tick._starts(c, ac, d))
    finally:
        c.close()


# ---------------------------------------------------------------------------

def test_nothing_active_does_nothing(tmp_path):
    make_exp(tmp_path)
    w = World(tmp_path)
    assert w.tick(datetime(2026, 11, 16, 22, 0, tzinfo=UTC)).status == "idle"
    (tmp_path / tick.ACTIVE_NAME).write_text("\n")
    assert w.tick(datetime(2026, 11, 16, 22, 0, tzinfo=UTC)).status == "idle"
    c = db.connect(tmp_path / "dev.db")
    assert c.execute("SELECT COUNT(*) FROM heartbeat").fetchone()[0] == 0
    c.close()


def test_due_at_the_fetch_time_not_before_and_idempotent(world):
    t = eq_fetch(START)
    r = world.tick(t - timedelta(seconds=1))
    assert r.status == "ok" and r.ran == []
    r = world.tick(t)
    assert [(x["asset_class"], x["date"], x["kind"], x["status"]) for x in r.ran] == [
        ("us_equity", START.isoformat(), "run", "ok")]
    calls = world.calls()
    r = world.tick(t + timedelta(minutes=40))                  # twice is safe: nothing runs again
    assert r.ran == [] and world.calls() == calls
    # crypto day 16 is due at 00:05Z on the 17th
    r = world.tick(cal.crypto_times(START).bars_fetch_at)
    assert [(x["asset_class"], x["date"]) for x in r.ran] == [("crypto", START.isoformat())]
    c = db.connect(conn_path)
    ticks = c.execute("SELECT event, detail FROM heartbeat WHERE job = 'tick' ORDER BY heartbeat_id").fetchall()
    assert len(ticks) == 4 and all(e == "finished" for e, _ in ticks)
    c.close()


def test_retry_cap_three_attempts_then_one_settle_only_run(world, monkeypatch):
    """A crash after the AI calls (before they are recorded) can cause at most 3 rounds of calls."""
    real = daily._Day._record

    def crash(self, aid, *a, **kw):
        if aid in self.ai_ids:
            raise RuntimeError("disk full after the AI calls")
        return real(self, aid, *a, **kw)
    monkeypatch.setattr(daily._Day, "_record", crash)
    t = eq_fetch(START)
    deadline = cal.decision_deadline("us_equity", START, db.connect(conn_path), TR.no_network)
    results = [world.tick(t + timedelta(minutes=10 * i)) for i in range(6)]          # all before the deadline
    eq_runs = [x for r in results for x in r.ran if x["asset_class"] == "us_equity"]
    assert [x["attempt"] for x in eq_runs] == [1, 2, 3] and all(x["status"] == "error" for x in eq_runs)
    assert all(r.exit_code == 1 for r in results[:3]) and results[3].exit_code == 0
    assert any(s["why"] == "attempts used" for s in results[5].skipped)
    calls = world.calls()
    assert max(calls.values()) == 3 and all(v <= 3 for v in calls.values())    # at most 3 rounds of calls
    assert calls["8"] == 3
    # past the deadline: one settle-only run, no AI call, then the day is left alone
    r = world.tick(deadline + timedelta(minutes=5))
    eq = [x for x in r.ran if x["asset_class"] == "us_equity"]
    assert [(x["kind"], x["attempt"]) for x in eq] == [("settle_only", 4)]
    assert world.calls() == calls
    r = world.tick(deadline + timedelta(minutes=10))
    assert not [x for x in r.ran if x["asset_class"] == "us_equity" and x["date"] == START.isoformat()]
    assert any(s["why"] == "gap: past the deadline" and s["date"] == START.isoformat() for s in r.skipped)
    assert started(conn_path, "us_equity", START) == 4
    c = db.connect(conn_path)
    assert c.execute("SELECT COUNT(*) FROM equity WHERE as_of_date = ?", (START.isoformat(),)).fetchone()[0] > 0
    c.close()


def test_catch_up_after_an_outage_runs_oldest_first(world):
    # the Pi was down from Mon 16 to Thu 19 Nov, 22:40Z: 16-18 are past their deadline, 19 is not
    world.market = CatchUpMarket
    r = world.tick(datetime(2026, 11, 19, 22, 40, tzinfo=UTC))
    eq = [(x["date"], x["kind"]) for x in r.ran if x["asset_class"] == "us_equity"]
    assert eq == [("2026-11-16", "settle_only"), ("2026-11-17", "settle_only"), ("2026-11-18", "settle_only"),
                  ("2026-11-19", "run")]
    cr = [(x["date"], x["kind"]) for x in r.ran if x["asset_class"] == "crypto"]
    assert cr == [("2026-11-16", "settle_only"), ("2026-11-17", "settle_only"), ("2026-11-18", "run")]
    c = db.connect(conn_path)
    for d in ("2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19"):     # every day marked: no ledger holes
        assert c.execute("SELECT COUNT(*) FROM equity WHERE as_of_date = ? AND agent_id = '1'", (d,)).fetchone()[0] == 1
    c.close()


def test_lock_keeps_two_ticks_apart(world):
    held = open(world.data_dir / tick.LOCK_NAME, "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert world.tick(eq_fetch(START)).status == "busy"
    finally:
        held.close()
    assert world.tick(eq_fetch(START)).status == "ok"


def test_no_ai_runs_rule_agents_only_and_calls_nothing(world):
    activate(world.data_dir, "dev --no-ai")
    r = world.tick(eq_fetch(START))
    assert r.status == "ok"
    r = world.tick(cal.crypto_times(START).bars_fetch_at)
    assert r.status == "ok"
    assert sum(world.calls(roster.EQUITY_AI + roster.CRYPTO_AI).values()) == 0
    c = db.connect(conn_path)
    agents = {x[0] for x in c.execute("SELECT DISTINCT agent_id FROM decisions")}
    assert agents == set(roster.EQUITY_RULE + roster.CRYPTO_RULE)
    marked = {x[0] for x in c.execute("SELECT DISTINCT agent_id FROM equity")}
    assert marked == set(roster.EQUITY_RULE + roster.CRYPTO_RULE)
    c.close()


@pytest.mark.parametrize("name,start,end", [("shakedown", "2026-11-16", "2027-01-08"),
                                            ("official", "2027-01-11", "2027-04-02")])
def test_no_ai_refused_on_shakedown_and_official(tmp_path, name, start, end):
    make_exp(tmp_path, name, date.fromisoformat(start), date.fromisoformat(end))
    activate(tmp_path, f"{name} --no-ai")
    w = World(tmp_path)
    r = w.tick(datetime(2026, 11, 16, 22, 0, tzinfo=UTC))
    assert r.status == "error" and "--no-ai is only allowed on a dev label" in r.errors[0] and r.ran == []
    with pytest.raises(ex.ExperimentError, match="only allowed on a 'dev' label"):
        c = db.connect(tmp_path / f"{name}.db")
        try:
            daily.run_day(c, "us_equity", date.fromisoformat(start), TR.make_deps(TR.Clock(LATE), TR.Market(TR.Clock(LATE)),
                                                                                  w.clients), ai=False)
        finally:
            c.close()


LATE = datetime(2027, 6, 1, tzinfo=UTC)


def test_activate_writes_dev_only(tmp_path):
    make_exp(tmp_path)
    assert tick.write_active(tmp_path, "dev", True).read_text() == "dev --no-ai\n"
    assert tick.read_active(tmp_path) == tick.Active("dev", True)
    for name in ("shakedown", "official"):
        with pytest.raises(tick.TickError, match="never activates"):
            tick.write_active(tmp_path, name, False)
    make_exp(tmp_path, "soak")
    c = sqlite3.connect(tmp_path / "soak.db")
    c.close()
    (tmp_path / tick.ACTIVE_NAME).write_text("dev --fast\n")
    with pytest.raises(tick.TickError, match="unknown flags"):
        tick.read_active(tmp_path)


def test_wrong_label_for_the_file_is_an_error(tmp_path):
    make_exp(tmp_path)
    (tmp_path / "dev.db").rename(tmp_path / "other.db")
    activate(tmp_path, "other")
    r = World(tmp_path).tick(datetime(2026, 11, 16, 22, 0, tzinfo=UTC))
    assert r.status == "error" and "labelled 'dev', not 'other'" in r.errors[0]


# ---------------------------------------------------------------------------
# The cached NYSE calendar: winter, the 14 Mar 2027 clock change, an early close
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not REAL_DB.exists(), reason="no real database")
@pytest.mark.parametrize("d,fetch", [
    (date(2027, 3, 12), datetime(2027, 3, 12, 22, 30, tzinfo=UTC)),    # Fri, EST: close 21:00Z
    (date(2027, 3, 15), datetime(2027, 3, 15, 21, 30, tzinfo=UTC)),    # Mon after the change, EDT: 20:00Z
    (date(2026, 12, 24), datetime(2026, 12, 24, 19, 30, tzinfo=UTC)),  # Christmas Eve early close, 13:00 ET
])
def test_due_times_from_the_cached_calendar(d, fetch):
    conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        exp = ex.Experiment("dev", d, None, d)
        before, _ = tick.due(conn, exp, fetch - timedelta(seconds=1), TR.no_network)
        at, _ = tick.due(conn, exp, fetch, TR.no_network)
        assert [x for x in before if x.asset_class == "us_equity"] == []
        assert [(x.day, x.kind) for x in at if x.asset_class == "us_equity"] == [(d, "run")]
        # the deadline is 1 h before the next open, on that day's clock
        nxt = cal.next_session(d, conn, TR.no_network)
        late, _ = tick.due(conn, exp, nxt.open - timedelta(hours=1) + timedelta(seconds=1), TR.no_network)
        assert [(x.day, x.kind) for x in late if x.asset_class == "us_equity"] == [(d, "settle_only")]
    finally:
        conn.close()


@pytest.mark.skipif(not REAL_DB.exists(), reason="no real database")
def test_no_equity_due_on_a_holiday_or_weekend():
    conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for d in (date(2026, 12, 25), date(2027, 3, 13), date(2027, 3, 14)):
            exp = ex.Experiment("dev", d, None, d)
            todo, _ = tick.due(conn, exp, datetime.combine(d + timedelta(days=1), datetime.min.time(), UTC)
                               + timedelta(hours=12), TR.no_network)
            assert [x for x in todo if x.asset_class == "us_equity"] == []
            assert [x.asset_class for x in todo] == ["crypto"]              # crypto runs every day
    finally:
        conn.close()


def test_daily_cli_no_ai_refused_on_shakedown(tmp_path, monkeypatch):
    path = make_exp(tmp_path, "shakedown", date(2026, 11, 16), date(2027, 1, 8))
    monkeypatch.setattr(daily, "real_deps", lambda: TR.make_deps(TR.Clock(LATE), TR.Market(TR.Clock(LATE)), {}))
    assert daily.main(["equity", "--db", str(path), "--date", "2026-11-16", "--no-ai"]) == 1


def test_gitignore_covers_the_active_file_and_lock():
    lines = (ROOT / ".gitignore").read_text().split()
    assert "data/active_experiment" in lines and "data/tick.lock" in lines
