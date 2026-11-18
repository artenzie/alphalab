"""Results report (analysis/results.py): late days counted and excludable (protocol v1.13)."""

import math
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
