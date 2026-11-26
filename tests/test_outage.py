"""Outage catch-up (protocol v1.17): a 5-day outage across Thanksgiving (Thu 26 Nov, closed) and
the early close (Fri 27 Nov, 13:00 ET), on the cached calendar.

Every scheduled day gets its marks; past-deadline days are holds (settled, marked, margin-checked
on the protocol schedule) and show as missed; a day that can't be marked holds back the later days
and raises error:<ac>:<date> once; days left in error by the code before v1.17 repair to missed
through the normal path without changing any ledger row.
"""

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data import calendar as cal  # noqa: E402
from data import db  # noqa: E402
from execution import ledger as L  # noqa: E402
from runner import alerts, daily, status, tick  # noqa: E402

import test_runner as TR  # noqa: E402
import test_tick as TT  # noqa: E402

UTC = timezone.utc
START, END = date(2026, 11, 16), date(2026, 11, 30)
OFF = datetime(2026, 11, 23, 20, 0, tzinfo=UTC)          # before Mon 23's 22:30Z fetch
BOOT = datetime(2026, 11, 28, 20, 5, tzinfo=UTC)         # Sat: 5 days later
THANKSGIVING, EARLY = date(2026, 11, 26), date(2026, 11, 27)
EQ_MISSED = [date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25)]
CR_MISSED = [date(2026, 11, 23) + timedelta(days=i) for i in range(4)]       # 23..26


def flaky(fails: dict):
    """CatchUpMarket whose next fails['us_equity'] equity ingests fail (Wi-Fi not up yet)."""
    class Flaky(TT.CatchUpMarket):
        def ingest(self, conn, asset_class, run_id, now):
            if fails.get(asset_class, 0) > 0:
                fails[asset_class] -= 1
                with conn:
                    conn.execute("INSERT INTO heartbeat (run_id, job, event, at, detail) VALUES (?, 'fetch_prices', "
                                 "'error', ?, ?)", (run_id, db.utc_ts(now()),
                                                    json.dumps({"error": "ConnectionError: no route to host"})))
                return 1
            return super().ingest(conn, asset_class, run_id, now)
    return Flaky


class World(TT.World):
    def tick(self, at, online=True):
        clock = TR.Clock(at)
        return tick.run_tick(self.data_dir, clock, lambda: TR.make_deps(clock, self.market(clock), self.clients),
                             trading=TR.no_network, is_online=lambda: online)


def setup(tmp_path, line="dev"):
    path = TT.make_exp(tmp_path, start=START, end=END)
    TT.activate(tmp_path, line)
    w = World(tmp_path, market=flaky({}))
    return path, w


def normal_week(w, path):
    """On-time ticks until the Pi goes off: equity 16-20 Nov, crypto 16-22 Nov."""
    c = db.connect(path)
    times = [cal.equity_times(d, c, TR.no_network).bars_fetch_at for d in TR.EQ_HIST if START <= d <= OFF.date()]
    c.close()
    times += [cal.crypto_times(START + timedelta(days=i)).bars_fetch_at for i in range(7)]
    for t in sorted(t for t in times if t < OFF):
        r = w.tick(t)
        assert r.status == "ok", r.errors


def ro(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def outcomes(path, now):
    st = status.summary(ro(path), now, trading=TR.no_network)
    return st, {(d["asset_class"], d["date"]): d["outcome"] for d in st.days}


def ledger_rows(c):
    return {t: [tuple(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY 1")] for t in TR.LEDGER_TABLES}


def opens(c, prefix):
    return [(r[0], r[1]) for r in c.execute("SELECT key, priority FROM alerts WHERE event = 'open' AND key LIKE ? "
                                            "ORDER BY alert_id", (prefix + "%",))]


def iso(ds):
    return [d.isoformat() for d in ds]


# ---------------------------------------------------------------------------

def test_outage_across_thanksgiving_and_early_close(tmp_path, monkeypatch):
    path, w = setup(tmp_path)
    normal_week(w, path)
    c = db.connect(path)
    assert c.execute("SELECT COUNT(*) FROM positions WHERE agent_id = '1' AND as_of_date = '2026-11-20'").fetchone()[0]

    # a forced margin breach for the SPY-only benchmark, the first time it is checked after boot
    real_check, box = L.margin_check, {"n": 1}

    def breach_once(equity, gross, values):
        if box["n"] and set(values) == {"SPY"}:
            box["n"] -= 1                    # as if equity were 20% of itself: cut to 40% of the new gross,
            weights, events = real_check(equity * 0.2, gross, values)    # as weights of the real equity
            return {s: x * 0.2 for s, x in weights.items()}, events
        return real_check(equity, gross, values)
    monkeypatch.setattr(L, "margin_check", breach_once)

    # 1. boot: Wi-Fi not up yet. Nothing runs and no attempt is used.
    r = w.tick(BOOT, online=False)
    assert r.status == "offline" and r.ran == [] and r.exit_code == 0
    assert {s["why"] for s in r.skipped} == {"offline"}
    assert TT.started(path, "us_equity", EQ_MISSED[0]) == 0

    # 2. online, but the first equity ingest still fails: 23 Nov can't be marked, later equity days wait
    w.market = flaky({"us_equity": 1})
    r = w.tick(BOOT + timedelta(minutes=5))
    eq = [(x["date"], x["kind"], x["status"]) for x in r.ran if x["asset_class"] == "us_equity"]
    assert eq == [("2026-11-23", "settle_only", "error")]
    assert {s["date"]: s["why"] for s in r.skipped if s["asset_class"] == "us_equity"} == {
        d: "waits for 2026-11-23's marks" for d in ("2026-11-24", "2026-11-25", "2026-11-27")}
    cr = [(x["date"], x["kind"], x["status"]) for x in r.ran if x["asset_class"] == "crypto"]
    assert cr == [(d, "settle_only", "missed") for d in iso(CR_MISSED)] + [("2026-11-27", "run", "ok")]
    assert c.execute("SELECT COUNT(*) FROM equity WHERE agent_id = '1' AND as_of_date = '2026-11-24'").fetchone()[0] == 0

    # 3. the next tick catches up: 23-25 missed (held), 26 closed, 27 a late run before Mon's deadline
    r = w.tick(BOOT + timedelta(minutes=10))
    eq = [(x["date"], x["kind"], x["status"], x["late"]) for x in r.ran if x["asset_class"] == "us_equity"]
    assert eq == [(d, "settle_only", "missed", False) for d in iso(EQ_MISSED)] + [("2026-11-27", "run", "ok", True)]
    assert r.exit_code == 0

    # 4. Sun and Mon: crypto 28 and 29 on time, equity Mon 30 on time
    for t in (cal.crypto_times(date(2026, 11, 28)).bars_fetch_at, cal.crypto_times(date(2026, 11, 29)).bars_fetch_at,
              cal.equity_times(END, c, TR.no_network).bars_fetch_at):
        assert w.tick(t).status == "ok"

    # every scheduled day has exactly one mark per agent; Thanksgiving has none for equity
    eq_days = [d for d in TR.EQ_HIST if START <= d <= END]
    assert THANKSGIVING not in eq_days and EARLY in eq_days
    for ac, days in (("us_equity", eq_days), ("crypto", [START + timedelta(days=i) for i in range(14)])):
        ids = daily.roster.rule_ids(ac) + daily.roster.ai_ids(ac)
        for d in days:
            n = dict(c.execute("SELECT agent_id, COUNT(*) FROM equity WHERE as_of_date = ? GROUP BY agent_id",
                               (d.isoformat(),)).fetchall())
            assert all(n.get(a) == 1 for a in ids), (ac, d, n)
    assert c.execute("SELECT COUNT(*) FROM equity WHERE agent_id = '1' AND as_of_date = ?",
                     (THANKSGIVING.isoformat(),)).fetchone()[0] == 0
    assert TT.started(path, "us_equity", THANKSGIVING) == 0

    # missed days: no snapshot, no decision, no error heartbeat after the first catch-up attempt
    for d in EQ_MISSED:
        assert daily.stored_snapshot(c, "us_equity", d) is None
    errs = c.execute("""SELECT job FROM heartbeat WHERE event = 'error' AND at > ?""",
                     (db.utc_ts(BOOT + timedelta(minutes=10)),)).fetchall()
    assert errs == []

    # the early close: 27 Nov marked at its 13:00 ET (18:00Z) close, its orders fill at Mon 30's open
    assert cal.session(EARLY, c, TR.no_network).close == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
    row = c.execute("SELECT symbol, mark_price FROM positions WHERE agent_id = '1' AND as_of_date = ?",
                    (EARLY.isoformat(),)).fetchone()
    assert row["mark_price"] == pytest.approx(TR.eq_close(row["symbol"], EARLY))
    late_snap = daily.stored_snapshot(c, "us_equity", EARLY)
    fills = c.execute("""SELECT DISTINCT f.filled_at FROM fills AS f JOIN orders AS o ON o.order_id = f.order_id
                         JOIN decisions AS d ON d.decision_id = o.decision_id
                         WHERE d.snapshot_id = ? AND f.status = 'filled'""", (late_snap["snapshot_id"],)).fetchall()
    assert [f[0] for f in fills] == [db.utc_ts(cal.session(END, c, TR.no_network).open)]

    # the margin check on a past-deadline day: stamped at 23 Nov's protocol decision time, the real
    # computation time in the risk event, filled at the 24 Nov open like any other order
    m = c.execute("""SELECT o.created_at, f.filled_at, f.status, e.occurred_at, e.detail FROM orders AS o
                     JOIN fills AS f ON f.order_id = o.order_id JOIN risk_events AS e ON e.risk_event_id = o.risk_event_id
                     WHERE o.agent_id = '1' AND o.origin = 'margin'""").fetchall()
    assert len(m) == 1 and box["n"] == 0
    decide23 = db.utc_ts(cal.equity_times(EQ_MISSED[0], c, TR.no_network).decide_at)
    assert m[0]["created_at"] == m[0]["occurred_at"] == decide23
    assert m[0]["status"] == "filled" and m[0]["filled_at"] == db.utc_ts(cal.session(EQ_MISSED[1], c, TR.no_network).open)
    assert json.loads(m[0]["detail"])["computed_at"] >= db.utc_ts(BOOT)

    # runner.status: missed, late and ok; never error
    now = cal.equity_times(END, c, TR.no_network).bars_fetch_at + timedelta(hours=1)
    st, o = outcomes(path, now)
    assert [o[("us_equity", d)] for d in iso(EQ_MISSED)] == ["missed"] * 3
    assert [o[("crypto", d)] for d in iso(CR_MISSED)] == ["missed"] * 4
    assert o[("us_equity", "2026-11-27")] == "late" and o[("crypto", "2026-11-27")] == "late"
    assert o[("us_equity", "2026-11-30")] == "ok" and o[("crypto", "2026-11-29")] == "ok"
    cnt = st.counts()
    assert cnt["error"] == 0 and cnt["missing"] == 0 and cnt["missed"] == 7
    assert "missed 7, error 0" in status.format_status(st)

    # alerts: each missed day opens once and is never re-opened, however many ticks check it
    for i in range(3):
        alerts.check_runs(c, now + timedelta(minutes=5 * i), trading=TR.no_network)
    keys = [k for k, _ in opens(c, "missed:")]
    assert sorted(keys) == sorted([f"missed:us_equity:{d}" for d in iso(EQ_MISSED)]
                                  + [f"missed:crypto:{d}" for d in iso(CR_MISSED)])
    assert opens(c, "error:") == []
    n = c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    alerts.check_runs(c, now + timedelta(minutes=20), trading=TR.no_network)
    assert c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == n
    c.close()


def test_a_day_that_keeps_failing_to_mark_alerts_once_and_holds_back_later_days(tmp_path):
    path, w = setup(tmp_path, "dev --no-ai")
    normal_week(w, path)
    c = db.connect(path)
    w.market = flaky({"us_equity": 3})
    for i in range(3):                                     # three settle-only runs, each fails to mark 23 Nov
        t = BOOT + timedelta(minutes=5 * i)
        r = w.tick(t)
        assert [(x["date"], x["status"]) for x in r.ran if x["asset_class"] == "us_equity"] == [("2026-11-23", "error")]
        alerts.check_runs(c, t + timedelta(minutes=1), trading=TR.no_network)
        stuck = opens(c, "error:us_equity:2026-11-23")
        assert stuck == ([] if i == 0 else [("error:us_equity:2026-11-23", alerts.HIGH)])   # once, high
    assert TT.started(path, "us_equity", date(2026, 11, 24)) == 0          # later days waited
    body = c.execute("SELECT body FROM alerts WHERE key = 'error:us_equity:2026-11-23'").fetchone()[0]
    assert "later equity days wait" in body and "manual correction" in body
    _, o = outcomes(path, BOOT + timedelta(minutes=12))
    assert o[("us_equity", "2026-11-23")] == "error"

    r = w.tick(BOOT + timedelta(minutes=15))                # the ingest works again
    assert [(x["date"], x["status"]) for x in r.ran if x["asset_class"] == "us_equity"] == [
        ("2026-11-23", "missed"), ("2026-11-24", "missed"), ("2026-11-25", "missed"), ("2026-11-27", "ok")]
    for i in range(2):
        alerts.check_runs(c, BOOT + timedelta(minutes=16 + i), trading=TR.no_network)
    ev = [r[0] for r in c.execute("SELECT event FROM alerts WHERE key = 'error:us_equity:2026-11-23' ORDER BY alert_id")]
    assert ev == ["open", "resolved"]
    _, o = outcomes(path, BOOT + timedelta(minutes=20))
    assert o[("us_equity", "2026-11-23")] == "missed"
    c.close()


def test_days_left_in_error_by_the_old_code_repair_to_missed_without_editing_rows(tmp_path, monkeypatch):
    """dev.db's 23/24 Nov: settled and marked, but the run ended in error and later days moved on."""
    path, w = setup(tmp_path, "dev --no-ai")
    normal_week(w, path)
    real = daily._Day.margin_only

    def old_code(self, *a, **kw):
        if self.day in EQ_MISSED[:2]:
            self.hb("decide", "error", reason="past the decision deadline: no agent decides today")
            return None
        return real(self, *a, **kw)
    monkeypatch.setattr(daily._Day, "margin_only", old_code)
    r = w.tick(BOOT)
    assert [(x["date"], x["status"]) for x in r.ran if x["asset_class"] == "us_equity"] == [
        ("2026-11-23", "error"), ("2026-11-24", "error"), ("2026-11-25", "missed"), ("2026-11-27", "ok")]
    monkeypatch.setattr(daily._Day, "margin_only", real)

    c = db.connect(path)
    before = ledger_rows(c)
    r = w.tick(BOOT + timedelta(minutes=5))                 # the normal path, after the fix
    assert [(x["date"], x["kind"], x["status"]) for x in r.ran if x["asset_class"] == "us_equity"] == [
        ("2026-11-23", "settle_only", "missed"), ("2026-11-24", "settle_only", "missed")]
    assert ledger_rows(c) == before                         # no row added, none changed
    run_id = c.execute("SELECT run_id FROM heartbeat WHERE job = 'daily_run' AND event = 'finished' "
                       "AND json_extract(detail, '$.status') = 'missed' ORDER BY heartbeat_id DESC LIMIT 1").fetchone()[0]
    checks = [json.loads(d) for (d,) in c.execute("SELECT detail FROM heartbeat WHERE run_id = ? AND job LIKE "
                                                  "'place_orders:%'", (run_id,))]
    assert checks and all(x["status"] == "checked_late" for x in checks)
    _, o = outcomes(path, BOOT + timedelta(minutes=10))
    assert [o[("us_equity", d)] for d in iso(EQ_MISSED)] == ["missed"] * 3
    assert w.tick(BOOT + timedelta(minutes=10)).ran == []   # final: left alone
    c.close()


def test_online_is_one_https_request_with_a_short_timeout():
    seen = []

    class Up:
        def get(self, url, timeout):
            seen.append((url, timeout))
            return object()

    class Down:
        def get(self, url, timeout):
            raise ConnectionError("Name or service not known")

    assert tick.online(Up()) is True and tick.online(Down()) is False
    assert seen == [("https://data.alpaca.markets/", 5)]
