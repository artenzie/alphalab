"""Results report from an experiment database (protocol §10): late and missed days counted, late days excludable.

    python -m analysis.results --experiment shakedown
    python -m analysis.results --experiment official --exclude-late
    python -m analysis.results --db /tmp/dev.db

Per agent, from the append-only log (newest row per day wins, so corrections apply):

  days          equity rows (settled days), the first one at $100,000
  decided       days whose snapshot the agent made a decision on
  late          of those, days whose snapshot was built late (v1.13: payload late = true)
  missed        settled days with no decision (no snapshot, failed checks, or past the deadline)
  return        total return over every daily return
  return_ex     with --exclude-late: total return compounding only the daily returns not
                driven by a late decision. The return of day D_k (equity D_k / equity D_k-1)
                comes from the positions decided on D_k-1, so it is dropped when D_k-1's
                snapshot was late.

This is the first piece of §10; the risk-adjusted metrics and the bootstrap come in Phase 6.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from runner import experiment as ex


@dataclass
class AgentResult:
    agent_id: str
    asset_class: str
    days: list[str] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)
    decided: set[str] = field(default_factory=set)
    late_days: set[str] = field(default_factory=set)       # decided on a late snapshot

    @property
    def missed(self) -> list[str]:
        return [d for d in self.days if d not in self.decided]

    def returns(self, exclude_late: bool = False) -> list[float]:
        out = []
        for k in range(1, len(self.days)):
            if exclude_late and self.days[k - 1] in self.late_days:
                continue
            out.append(self.equity[k] / self.equity[k - 1] - 1)
        return out

    def total_return(self, exclude_late: bool = False) -> float | None:
        r = self.returns(exclude_late)
        return math.prod(1 + x for x in r) - 1 if self.days else None


def late_snapshots(conn: sqlite3.Connection) -> dict[tuple[str, str], bool]:
    """(asset_class, snapshot_date) -> late, for the current (uncorrected) snapshot of each day."""
    out = {}
    for r in conn.execute("SELECT asset_class, snapshot_date, payload FROM snapshots WHERE corrects_id IS NULL"):
        out[(r["asset_class"], r["snapshot_date"])] = bool(json.loads(r["payload"]).get("late", False))
    return out


def load(conn: sqlite3.Connection) -> dict[str, AgentResult]:
    late = late_snapshots(conn)
    res: dict[str, AgentResult] = {}
    for r in conn.execute(
            """SELECT e.agent_id, a.asset_class, e.as_of_date, e.equity FROM equity AS e
               JOIN agents AS a ON a.agent_id = e.agent_id
               WHERE e.equity_id = (SELECT MAX(q.equity_id) FROM equity AS q
                                    WHERE q.agent_id = e.agent_id AND q.as_of_date = e.as_of_date)
               ORDER BY e.agent_id, e.as_of_date"""):
        a = res.setdefault(r["agent_id"], AgentResult(r["agent_id"], r["asset_class"]))
        a.days.append(r["as_of_date"])
        a.equity.append(r["equity"])
    for r in conn.execute(
            """SELECT DISTINCT d.agent_id, s.asset_class, s.snapshot_date FROM decisions AS d
               JOIN snapshots AS s ON s.snapshot_id = d.snapshot_id
               WHERE d.corrects_id IS NULL AND s.corrects_id IS NULL"""):
        a = res.get(r["agent_id"])
        if a is None:
            continue
        a.decided.add(r["snapshot_date"])
        if late.get((r["asset_class"], r["snapshot_date"])):
            a.late_days.add(r["snapshot_date"])
    return res


def _order(aid: str) -> tuple:
    return (aid.startswith("C"), int(aid.lstrip("C")) if aid.lstrip("C").isdigit() else 0, aid)


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:+.2f}%"


def report(conn: sqlite3.Connection, exclude_late: bool = False) -> str:
    res = load(conn)
    exp = ex.get(conn)
    head = f"experiment {exp.name!r}, start {exp.start_date}" if exp else "unlabelled database"
    lines = [head, ""]
    cols = f"{'agent':<6} {'class':<10} {'days':>5} {'decided':>8} {'late':>5} {'missed':>7} {'return':>9}"
    if exclude_late:
        cols += f" {'excl. late':>11} {'n used':>7}"
    lines.append(cols)
    for aid in sorted(res, key=_order):
        a = res[aid]
        line = (f"{aid:<6} {a.asset_class:<10} {len(a.days):>5} {len(a.decided & set(a.days)):>8} "
                f"{len(a.late_days):>5} {len(a.missed):>7} {_pct(a.total_return()):>9}")
        if exclude_late:
            line += f" {_pct(a.total_return(True)):>11} {len(a.returns(True)):>7}"
        lines.append(line)
    late_total = sorted({d for a in res.values() for d in a.late_days})
    lines += ["", f"late days: {len(late_total)}" + (f" ({', '.join(late_total)})" if late_total else "")]
    if exclude_late:
        lines.append("excl. late: each day's return driven by a late decision (the day after it) is left out")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Results report: returns per agent, late and missed days.")
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--experiment")
    where.add_argument("--db", type=Path)
    p.add_argument("--exclude-late", action="store_true", help="also report returns without late-decision days")
    args = p.parse_args(argv)
    path = args.db or ex.path_for(args.experiment)
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        print(report(conn, args.exclude_late))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
