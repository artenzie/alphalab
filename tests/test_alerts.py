"""runner/alerts.py: the alert queue, problems and recoveries, summary, spend/credit, pings, topic."""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from data import calendar as cal  # noqa: E402
from data import db  # noqa: E402
from runner import alerts, daily  # noqa: E402

import test_tick as TT  # noqa: E402
from test_tick import World, activate, make_exp  # noqa: E402

TR = TT.TR
UTC = timezone.utc


class Phone:
    def __init__(self, fail=0):
        self.got, self.fail = [], fail

    def __call__(self, title, body, priority, tags=""):
        if self.fail:
            self.fail -= 1
            raise alerts.SendError("ntfy: HTTP 503")
        self.got.append((title, body, priority))


def check(data_dir, at, phone, marker=None, spend=False, session=None):
    hooks = alerts.Hooks(send=phone, trading=TR.no_network, marker=marker or data_dir / "no-marker.json")
    if session is not None:
        hooks.session = session
    return alerts.after_tick(data_dir, at, lambda: at, hooks, spend=spend)


def rows(path):
    c = db.connect(path)
    try:
        return [dict(r) for r in c.execute("SELECT key, event, priority, title, body FROM alerts ORDER BY alert_id")]
    finally:
        c.close()


def fetch16(path):
    c = db.connect(path)
    try:
        return cal.equity_times(date(2026, 11, 16), c, TR.no_network).bars_fetch_at
    finally:
        c.close()


@pytest.fixture
def setup(tmp_path):
    path = make_exp(tmp_path)
    activate(tmp_path, "dev")
    return tmp_path, path, World(tmp_path, market=TT.CatchUpMarket)


def test_outage_missed_once_recovered_once_and_summaries(setup):
    data_dir, path, w = setup
    phone = Phone()
    t = fetch16(path)
    w.tick(t)                                                    # Mon 16 equity: on time, ok
    check(data_dir, t + timedelta(minutes=40), phone)
    summ = [r for r in rows(path) if r["key"] == "summary:us_equity:2026-11-16"]
    assert len(summ) == 1 and summ[0]["priority"] == alerts.LOW
    assert "equity 2026-11-16: ok, on time" in summ[0]["body"] and "backup: never ran" in summ[0]["body"]
    assert "  1  $" in summ[0]["body"] and "AI cost $" in summ[0]["body"]
    # the Pi is off until Wed 18 Nov 13:35Z: equity 17 and crypto 16 pass their deadlines
    now = datetime(2026, 11, 18, 13, 35, tzinfo=UTC)
    check(data_dir, now, phone)
    keys = {r["key"]: r for r in rows(path) if r["event"] == "open"}
    assert set(keys) == {"missed:us_equity:2026-11-17", "missed:crypto:2026-11-16"}
    assert all(r["priority"] == alerts.HIGH for r in keys.values())
    assert "no run started at all" in keys["missed:crypto:2026-11-16"]["body"]
    n = len(rows(path))
    check(data_dir, now + timedelta(minutes=1), phone)           # deduped: nothing new
    assert len(rows(path)) == n
    # back on: settle-only runs for the missed days; crypto 17 runs late and ok
    w.tick(datetime(2026, 11, 18, 13, 40, tzinfo=UTC))
    check(data_dir, datetime(2026, 11, 18, 13, 45, tzinfo=UTC), phone)
    new = rows(path)[n:]
    res = [r for r in new if r["event"] == "resolved"]
    assert [r["key"] for r in res] == ["missed:crypto:2026-11-16"]
    assert res[0]["priority"] == alerts.LOW and "2026-11-16 stays a gap" in res[0]["body"]
    summ17 = [r for r in new if r["key"] == "summary:us_equity:2026-11-17"]
    assert len(summ17) == 1 and "MISSED" in summ17[0]["body"] and "crypto 2026-11-17: ok (late)" in summ17[0]["body"]
    assert alerts.is_open(db.connect(path), "missed:us_equity:2026-11-17")
    assert len(phone.got) == len(rows(path))                     # every row sent once
    sends = db.connect(path).execute("SELECT COUNT(*) FROM alert_sends").fetchone()[0]
    assert sends == len(rows(path))


def test_run_error_alerts_once_then_resolves(setup, monkeypatch):
    data_dir, path, w = setup
    phone = Phone()
    real = daily._Day._record
    boom = {"n": 1}

    def crash(self, aid, *a, **kw):
        if aid in self.ai_ids and boom["n"]:
            boom["n"] = 0
            raise RuntimeError("disk full after the AI calls")
        return real(self, aid, *a, **kw)
    monkeypatch.setattr(daily._Day, "_record", crash)
    t = fetch16(path)
    r = w.tick(t)
    assert any(x["status"] == "error" for x in r.ran)
    check(data_dir, t + timedelta(minutes=35), phone)
    opened = [x for x in rows(path) if x["event"] == "open"]
    assert [x["key"] for x in opened] == ["error:us_equity:2026-11-16"]
    assert "run 1 of 3" in opened[0]["body"] and "disk full" in opened[0]["body"]
    check(data_dir, t + timedelta(minutes=36), phone)
    assert len([x for x in rows(path) if x["event"] == "open"]) == 1
    w.tick(t + timedelta(minutes=40))                            # the retry succeeds
    check(data_dir, t + timedelta(minutes=80), phone)
    res = [x for x in rows(path) if x["event"] == "resolved"]
    assert [x["key"] for x in res] == ["error:us_equity:2026-11-16"]
    assert any(x["key"] == "summary:us_equity:2026-11-16" for x in rows(path))


def test_tick_failure_opens_and_resolves(setup):
    data_dir, path, _ = setup
    c = db.connect(path)
    at = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    for status_, errors in (("error", ["TickError: boom", "trace"]), ("error", ["TickError: boom"]), ("ok", [])):
        with c:
            c.execute("INSERT INTO heartbeat (run_id, job, event, at, detail) VALUES ('x', 'tick', 'finished', ?, ?)",
                      (db.utc_ts(at), json.dumps({"status": status_, "errors": errors, "ran": []})))
        alerts.check_tick(c, at)
    ev = [(r[0], r[1]) for r in c.execute("SELECT key, event FROM alerts")]
    assert ev == [("tick", "open"), ("tick", "resolved")]
    c.close()


def test_a_failed_send_stays_pending_and_is_retried(setup):
    data_dir, path, _ = setup
    c = db.connect(path)
    at = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    alerts.queue(c, "a", "notice", "one", "b", 3, at)
    alerts.queue(c, "b", "notice", "two", "b", 3, at)
    phone = Phone(fail=1)
    assert alerts.flush(c, phone, lambda: at) == (0, 2)
    assert alerts.flush(c, phone, lambda: at) == (2, 0)
    assert [g[0] for g in phone.got] == ["one", "two"]
    assert alerts.flush(c, phone, lambda: at) == (0, 0)
    assert alerts.flush(c, None, lambda: at) == (0, 0)
    c.close()


def test_alert_failures_never_raise(setup, monkeypatch):
    data_dir, path, _ = setup
    monkeypatch.setattr(alerts, "check_runs", lambda *a, **k: 1 / 0)
    assert check(data_dir, datetime(2026, 11, 16, 12, 0, tzinfo=UTC), Phone()) == (0, 0)


class Resp:
    def __init__(self, code, data):
        self.status_code, self._data = code, data

    def json(self):
        return {"data": self._data}


class Session:
    def __init__(self, key, credits, code=200):
        self.key, self.credits, self.code, self.headers = key, credits, code, []

    def get(self, url, headers=None, timeout=None):
        self.headers.append(headers)
        return Resp(self.code, self.key if url.endswith("/key") else self.credits)


def spend_rows(path):
    return [(r["key"], r["event"], r["priority"]) for r in rows(path) if r["key"].startswith(("spend", "credit"))]


def test_spend_and_credit(setup, monkeypatch, capsys):
    data_dir, path, _ = setup
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret-123")
    monkeypatch.delenv("OPENROUTER_MONTHLY_CAP", raising=False)
    c = db.connect(path)
    at = datetime(2026, 12, 3, 12, 0, tzinfo=UTC)
    s = Session({"limit": 20, "usage_monthly": 15.9}, {"total_credits": 30, "total_usage": 20})
    alerts.check_spend(c, at, s)
    assert spend_rows(path) == []
    s.key["usage_monthly"] = 16.0                                  # 80% of 20
    s.credits["total_usage"] = 25.5                                # $4.50 left
    alerts.check_spend(c, at, s)
    alerts.check_spend(c, at, s)
    assert spend_rows(path) == [("spend:2026-12", "notice", 3), ("credit_low", "open", 3)]
    s.credits["total_credits"] = 50                                # topped up
    alerts.check_spend(c, at, s)
    s.credits["total_usage"] = 46                                  # below again: a new crossing
    alerts.check_spend(c, at, s)
    assert spend_rows(path)[2:] == [("credit_low", "resolved", 2), ("credit_low", "open", 3)]
    # no limit on the key: OPENROUTER_MONTHLY_CAP; next month alerts again
    s.key = {"limit": None, "usage_monthly": 12.5}
    monkeypatch.setenv("OPENROUTER_MONTHLY_CAP", "15")
    alerts.check_spend(c, datetime(2027, 1, 2, tzinfo=UTC), s)
    assert ("spend:2027-01", "notice", 3) in spend_rows(path)
    assert "$12.50 of $15.00 (OPENROUTER_MONTHLY_CAP)" in [r for r in rows(path) if r["key"] == "spend:2027-01"][0]["body"]
    # an error reply is logged, the key never
    alerts.check_spend(c, at, Session({}, {}, code=401))
    err = capsys.readouterr().err
    assert "HTTP 401" in err and "sk-or-secret" not in err
    assert all(h == {"Authorization": "Bearer sk-or-secret-123"} for h in s.headers)
    c.close()


def test_backup_line():
    now = datetime(2026, 12, 14, 22, 0, tzinfo=UTC)
    assert "never ran" in alerts.backup_line(now, None)
    ok = {"date": "2026-12-14", "status": "ok", "finished_at": "2026-12-14T02:31:00.000Z"}
    assert alerts.backup_line(now, ok) == "backup 2026-12-14: ok"
    bad = {"date": "2026-12-14", "status": "failed", "step": "rclone copy", "finished_at": "2026-12-14T02:31:00.000Z"}
    assert alerts.backup_line(now, bad) == "backup 2026-12-14: failed at rclone copy"
    old = {**ok, "date": "2026-12-11", "finished_at": "2026-12-11T02:31:00.000Z"}
    assert "NONE SINCE" in alerts.backup_line(now, old)


def test_ping_skips_without_url_and_pinger_pings(monkeypatch):
    monkeypatch.delenv("HEALTHCHECKS_URL", raising=False)
    assert alerts.ping() is False
    hits = []
    with alerts.Pinger(every=0.01, ping_fn=lambda: hits.append(1)):
        import time
        time.sleep(0.05)
    assert len(hits) >= 2


def test_ntfy_sender_error_hides_topic():
    class S:
        def post(self, url, json=None, timeout=None):
            self.sent = (url, json)
            return Resp(429, None)
    s = S()
    send = alerts.ntfy_sender("alphalab-secrettopic", s)
    with pytest.raises(alerts.SendError) as e:
        send("t", "b", 4, "warning")
    assert "secrettopic" not in str(e.value) and "429" in str(e.value)
    assert s.sent[1] == {"topic": "alphalab-secrettopic", "title": "t", "message": "b", "priority": 4,
                         "tags": ["warning"]}


def test_init_topic_writes_once(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OPENROUTER_API_KEY=x")                       # no trailing newline
    topic = alerts.init_topic(env)
    assert topic.startswith("alphalab-") and len(topic) == 29
    assert env.read_text() == f"OPENROUTER_API_KEY=x\nNTFY_TOPIC={topic}\n"
    assert alerts.init_topic(env) is None
    assert env.read_text().count("NTFY_TOPIC") == 1


def test_notify_queues_dedupes_and_sends(setup):
    data_dir, path, _ = setup
    phone = Phone()
    at = datetime(2026, 12, 14, 2, 31, tzinfo=UTC)
    msg = alerts.notify(data_dir, "backup:2026-12-14", "Backup FAILED", "rclone copy", 4, lambda: at, phone)
    assert "queued" in msg and phone.got == [("Backup FAILED", "rclone copy", 4)]
    alerts.notify(data_dir, "backup:2026-12-14", "Backup FAILED", "rclone copy", 4, lambda: at, phone)
    assert len(phone.got) == 1
    (data_dir / "active_experiment").write_text("")
    assert "directly" in alerts.notify(data_dir, "k", "t", "b", 4, lambda: at, phone)
