"""Results report (analysis/results.py): late days counted and excludable (protocol v1.13)."""

import math

import pytest
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from analysis import results  # noqa: E402
from data import calendar as cal  # noqa: E402
from test_runner import DAYS, clients, conn, equity_run  # noqa: E402,F401


def test_late_days_are_counted_and_can_be_excluded(conn, clients):
    equity_run(conn, DAYS[0], clients)
    late_at = cal.equity_times(DAYS[1], conn).snapshot_at + timedelta(hours=3)
    r, _ = equity_run(conn, DAYS[1], clients, at=late_at)                  # day 2 decided late
    assert r.late
    equity_run(conn, DAYS[2], clients)
    equity_run(conn, DAYS[3], clients, at=cal.decision_deadline("us_equity", DAYS[3], conn) + timedelta(minutes=5))
    res = results.load(conn)
    d = [x.isoformat() for x in DAYS[:4]]
    for aid in ("1", "8"):
        a = res[aid]
        assert a.days == d
        assert a.late_days == {d[1]}
        assert a.missed == [d[3]]                                         # past the deadline: no decision
        all_r = a.returns()
        assert len(all_r) == 3 and len(a.returns(exclude_late=True)) == 2
        # the return of day 3 comes from day 2's (late) decision: it is the one left out
        kept = [x for k, x in enumerate(all_r, start=1) if k != 2]
        assert a.returns(exclude_late=True) == kept
        assert a.total_return(True) == math.prod(1 + x for x in kept) - 1
    text = results.report(conn, exclude_late=True)
    assert f"late days: 1 ({d[1]})" in text and "excl. late" in text
    line = next(l for l in text.splitlines() if l.startswith("8 "))
    assert line.split()[2:6] == ["4", "3", "1", "1"]                       # days, decided, late, missed


def test_cli_reads_the_database_read_only(conn, clients, tmp_path, capsys):
    equity_run(conn, DAYS[0], clients)
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    assert results.main(["--db", path, "--exclude-late"]) == 0
    out = capsys.readouterr().out
    assert "experiment 'dev'" in out and "late days: 0" in out


def test_overlap_is_same_direction_sum_of_min_weights():
    a = {"AAPL": 0.15, "NVDA": 0.15, "XOM": -0.10, "JPM": 0.10}
    b = {"AAPL": 0.10, "NVDA": 0.20, "XOM": -0.05, "JPM": -0.10}
    assert results.overlap(a, b) == pytest.approx(0.10 + 0.15 + 0.05)     # JPM long vs short: none
    assert results.overlap(a, a) == pytest.approx(0.50)
    assert results.overlap(a, {}) == 0.0 and results.overlap(b, a) == results.overlap(a, b)


def test_daily_overlap_uses_risk_checked_targets_and_carries_holds(conn, clients, monkeypatch):
    import test_runner
    from test_runner import Client
    monkeypatch.setitem(test_runner.AI_TARGETS, "9", {"AAPL": 0.10, "NVDA": 0.20, "XOM": -0.05})
    monkeypatch.setitem(test_runner.AI_TARGETS, "11", {"AMZN": 0.30})      # clipped to 20% by risk
    equity_run(conn, DAYS[0], clients)
    down = dict(clients, **{"9": Client("9", fail=True)})                   # agent 9 unavailable: holds
    equity_run(conn, DAYS[1], down)
    by_day = results.overlap_by_day(conn)
    d0, d1 = DAYS[0].isoformat(), DAYS[1].isoformat()
    pairs = 10                                                              # headline: agents 8-12 only
    assert len(by_day[d0]) == pairs and {a for p in by_day[d0] for a in p} == set(results.HERDING_HEADLINE)
    assert by_day[d0][("8", "9")] == pytest.approx(0.30)
    assert by_day[d0][("11", "12")] == pytest.approx(0.20)                  # both AMZN at the 20% cap
    assert by_day[d1][("8", "9")] == pytest.approx(0.30)                    # 9's day-1 targets carried
    text = results.report(conn)
    assert "AI target overlap" in text and f"{d0}    {pairs}" in text


def test_reasoning_summary_and_gate(conn, clients, monkeypatch):
    import test_runner
    from runner import roster
    from test_runner import Client
    monkeypatch.setattr(roster, "SHADOW_EXPERIMENT", "dev")
    monkeypatch.setattr(roster, "SHADOW_START", DAYS[0])
    clients = dict(clients, **{roster.SHADOW_KEY: Client("8")})
    assert results.reasoning_gate(conn).verdict == "no data"
    equity_run(conn, DAYS[0], clients)                                     # shadow = agent 8's book
    g = results.reasoning_gate(conn)
    assert (g.days, g.verdict) == (1, "high") and g.mean_overlap == pytest.approx(0.40)   # gross 0.4 < 0.6
    monkeypatch.setitem(test_runner.AI_TARGETS, "8", {"AAPL": 0.2, "NVDA": 0.2, "JPM": 0.2, "XOM": -0.1})
    equity_run(conn, DAYS[1], clients)
    g = results.reasoning_gate(conn)
    assert g.days == 2 and g.per_day[DAYS[1].isoformat()] == pytest.approx(0.70)
    assert g.mean_overlap == pytest.approx(0.55) and g.verdict == "high"
    equity_run(conn, DAYS[2], clients)                                     # mean exactly 0.6: not below
    g = results.reasoning_gate(conn)
    assert g.days == 3 and g.mean_overlap == pytest.approx(0.60) and g.verdict == "medium"
    rs = results.reasoning_summary(conn)
    assert rs["8"] == (3, 0, 0.0)                                          # the fake replies report 0
    text = results.report(conn)
    assert "effort medium" in text and "with reasoning   0%" in text


def test_supplementary_overlap_covers_ai_call_agents_and_never_13(conn, clients):
    equity_run(conn, DAYS[0], clients)
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE agent_id = '13' AND parsed_targets <> 'null'"
                        ).fetchone()[0] == 1                               # 13 has a book that day ...
    sup = results.overlap_by_day(conn, results.HERDING_SUPPLEMENTARY)[DAYS[0].isoformat()]
    agents = {a for p in sup for a in p}
    assert agents == {"7", "8", "9", "10", "11", "12", "14", "15"} and len(sup) == 28
    assert "13" not in agents                                              # ... but is in neither figure
    head = results.overlap_by_day(conn)[DAYS[0].isoformat()]
    assert set(head) <= set(sup) and len(head) == 10
    assert all(head[k] == sup[k] for k in head)
    text = results.report(conn)
    assert "headline: agents 8-12" in text and "supplementary: agents 7-12, 14, 15; 13 excluded" in text
    assert f"{DAYS[0].isoformat()}    28" in text and f"{DAYS[0].isoformat()}    10" in text


def test_reasoning_summary_ignores_rows_without_a_call(conn, clients):
    for d in DAYS[:3]:
        equity_run(conn, d, clients)
    calls, _, _ = results.reasoning_summary(conn)["13"]
    assert calls == 1                                                      # day 1 only; follow/hold rows left out
