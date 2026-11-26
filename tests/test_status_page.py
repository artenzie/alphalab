"""dashboard/status_page.py: the data function, read-only access, escaping, binding, one request."""

import hashlib
import json
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dashboard import status_page as sp  # noqa: E402
from data import db  # noqa: E402

import test_tick as TT  # noqa: E402
from test_tick import World, activate, make_exp  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)


def ts(d, hh="21:00"):
    return f"{d}T{hh}:00.000Z"


def snapshot(c, ac, d):
    return c.execute("""INSERT INTO snapshots (asset_class, snapshot_date, cutoff_ts, built_at, payload, sha256,
                        checks, checks_passed) VALUES (?, ?, ?, ?, '{}', ?, '{}', 1)""",
                     (ac, d, ts(d), ts(d, "21:01"), "0" * 64)).lastrowid


def decision(c, agent, snap, d, status, attempt=1, cost=None, error=None):
    targets = json.dumps({"SPY": 1.0}) if status == "ok" else None
    c.execute("""INSERT INTO decisions (agent_id, snapshot_id, attempt, decided_at, status, cost_usd,
                 parsed_targets, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
              (agent, snap, attempt, ts(d, f"21:0{1 + attempt}"), status, cost, targets, error))


def equity(c, agent, d, eq, corrects=None):
    return c.execute("INSERT INTO equity (agent_id, as_of_date, cash, equity, corrects_id) VALUES (?, ?, ?, ?, ?)",
                     (agent, d, eq, eq, corrects)).lastrowid


def alert(c, key, event, title, at):
    c.execute("INSERT INTO alerts (key, event, title, body, priority, created_at) VALUES (?, ?, ?, 'b', 4, ?)",
              (key, event, title, ts(at)))


@pytest.fixture
def exp(tmp_path):
    path = make_exp(tmp_path)
    activate(tmp_path, "dev --no-ai")
    c = db.connect(path)
    with c:
        s_oct = snapshot(c, "us_equity", "2026-10-30")             # last month: not in month to date
        s_19 = snapshot(c, "us_equity", "2026-11-19")
        s_20 = snapshot(c, "us_equity", "2026-11-20")
        c_20 = snapshot(c, "crypto", "2026-11-20")
        decision(c, "8", s_oct, "2026-10-30", "ok", cost=5.0)
        decision(c, "8", s_19, "2026-11-19", "ok", cost=0.25)
        decision(c, "8", s_20, "2026-11-20", "invalid", 1, cost=0.10, error="bad JSON " + "x" * 300)
        decision(c, "8", s_20, "2026-11-20", "ok", 2, cost=0.20)
        decision(c, "9", s_20, "2026-11-20", "unavailable", 1, error="timeout")
        decision(c, "C1", c_20, "2026-11-20", "ok", cost=0.05)
        decision(c, "1", s_20, "2026-11-20", "ok")                  # rule agent: not in the AI table
        equity(c, "1", "2026-11-19", 100_000.0)
        e = equity(c, "1", "2026-11-20", 101_000.0)
        equity(c, "1", "2026-11-20", 102_500.0, corrects=e)         # the correction wins
        equity(c, "8", "2026-11-20", 97_000.0)
        alert(c, "error:us_equity:2026-11-19", "open", "Equity run error <script>alert(1)</script>", "2026-11-19")
        alert(c, "missed:crypto:2026-11-18", "open", "Crypto missed", "2026-11-18")
        alert(c, "missed:crypto:2026-11-18", "resolved", "Crypto recovered", "2026-11-19")
        alert(c, "summary:us_equity:2026-11-19", "notice", "Daily summary", "2026-11-19")
    c.close()
    marker = tmp_path / "last.json"
    marker.write_text(json.dumps({"date": "2026-11-20", "status": "ok", "finished_at": ts("2026-11-20", "02:31")}))
    return tmp_path, path, marker


def load(exp, now=NOW):
    data_dir, _, marker = exp
    return sp.load(data_dir, now, marker, trading=TT.TR.no_network)


def test_page_data(exp):
    d = load(exp)
    assert d["errors"] == {}
    assert d["experiment"] == "dev" and d["no_ai"] is True
    eq = {r["agent_id"]: r for r in d["equity"]}
    assert len(eq) == 17
    assert eq["1"]["as_of"] == "2026-11-20" and eq["1"]["equity"] == 102_500.0
    assert eq["1"]["return"] == pytest.approx(0.025)
    assert eq["8"]["return"] == pytest.approx(-0.03)
    assert eq["2"]["equity"] is None and eq["2"]["return"] is None
    ds = {r["agent_id"]: r for r in d["decisions"]}
    assert set(ds) == {"7", "8", "9", "10", "11", "12", "13", "14", "15", "C1"}
    assert (ds["8"]["date"], ds["8"]["status"], ds["8"]["attempt"]) == ("2026-11-20", "ok", 2)
    assert (ds["9"]["status"], ds["9"]["error"]) == ("unavailable", "timeout")
    assert ds["C1"]["status"] == "ok" and ds["7"]["status"] is None
    assert d["cost_today"][0] == pytest.approx(0.35) and d["cost_today"][1] == 3
    assert d["cost_month"][0] == pytest.approx(0.60) and d["cost_month"][1] == 4
    assert [a["key"] for a in d["alerts"]] == ["error:us_equity:2026-11-19"]
    assert d["backup"] == "backup 2026-11-20: ok"
    assert d["status"].experiment.name == "dev"


def test_long_errors_are_cut(exp):
    data_dir, path, _ = exp
    c = db.connect(path)
    with c:
        s = c.execute("SELECT snapshot_id FROM snapshots WHERE asset_class = 'us_equity' AND snapshot_date = "
                      "'2026-11-20'").fetchone()[0]
        decision(c, "10", s, "2026-11-20", "invalid", error="y" * 500)
    c.close()
    err = {r["agent_id"]: r for r in load(exp)["decisions"]}["10"]["error"]
    assert err == "y" * sp.ERROR_CHARS + "..."


def test_stale_backup_and_no_marker(exp):
    data_dir, _, marker = exp
    assert "NONE SINCE" in load(exp, NOW + timedelta(days=3))["backup"]
    marker.unlink()
    assert load(exp)["backup"] == "backup: never ran (no marker)"


def test_read_only_and_db_unchanged(exp):
    _, path, _ = exp
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    conn = sp.open_ro(path)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("INSERT INTO alerts (key, event, title, body, priority, created_at) "
                     "VALUES ('k', 'notice', 't', 'b', 1, ?)", (ts("2026-11-20"),))
    conn.close()
    load(exp)
    sp.render(load(exp))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_uncached_calendar_day_is_a_section_error_not_a_fetch(tmp_path):
    path = make_exp(tmp_path, end=date(2031, 1, 31))           # runs past the seeded calendar
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    conn = sp.open_ro(path)
    try:
        d = sp.page_data(conn, datetime(2031, 1, 6, 12, tzinfo=UTC), None)     # default: cache only
    finally:
        conn.close()
    assert "cached calendar only" in d["errors"]["status"] and d["status"] is None
    assert d["equity"] is not None                                             # the rest still shows
    assert "error:" in sp.render(d)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_status_section_after_a_real_tick(tmp_path):
    path = make_exp(tmp_path)
    activate(tmp_path, "dev")
    w = World(tmp_path, market=TT.CatchUpMarket)
    fetch = TT.eq_fetch(TT.START, db.connect(path))
    w.tick(fetch)
    d = sp.load(tmp_path, fetch + timedelta(minutes=40), tmp_path / "none.json", trading=TT.TR.no_network)
    assert d["errors"] == {}
    assert d["status"].last_runs["us_equity"]["status"] == "ok"
    assert d["cost_today"][1] >= 0
    html = sp.render(d)
    assert "Last run:" in html and "Uptime" in html


def test_render_escapes(exp):
    page = sp.render(load(exp))
    assert "<script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "+2.50%" in page and "$102,500.00" in page and "$0.3500" in page


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "0:0::0", "localhost", ""])
def test_wildcard_and_names_refused(host):
    with pytest.raises(sp.PageError):
        sp.check_host(host)


def test_interface_ipv4():
    assert sp.interface_ipv4("lo") == "127.0.0.1"
    assert sp.interface_ipv4("no-such-if0") is None


def test_no_active_experiment(tmp_path):
    with pytest.raises(sp.PageError, match="no active experiment"):
        sp.load(tmp_path, NOW, tmp_path / "x.json")


def test_one_request(exp):
    data_dir, _, marker = exp
    srv = sp.make_server("127.0.0.1", 0, data_dir, marker, clock=lambda: NOW, trading=TT.TR.no_network)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/") as r:
            assert r.status == 200
            assert "AlphaLab status" in r.read().decode()
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/x")
        assert e.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base + "/", data=b"x", method="POST"))
        assert e.value.code == 405
    finally:
        srv.shutdown()
        srv.server_close()
