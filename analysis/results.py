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

AI target overlap (herding), per equity snapshot day, for every pair of AI agents:

    overlap(a, b) = sum over stocks of min(long_a, long_b) + min(short_a, short_b)

with each agent's target weights after the risk checks (the weights its orders were
sized from). Longs only overlap with longs and shorts with shorts, so it is the share of
portfolio value both agents hold in the same stock in the same direction: 0 = nothing in
common, 1 = the same fully invested book. A hold, invalid or unavailable decision keeps
the agent's last targets (it holds them); before its first targets it has none and its
pairs are left out that day. The report gives the daily mean, min and max over pairs and
each pair's mean over the days.

This is the first piece of §10; the risk-adjusted metrics and the bootstrap come in Phase 6.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from statistics import mean
from typing import Mapping

from agents.models import AGENT_MODELS
from runner import experiment as ex
from runner.daily import applied_weights


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


def overlap(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    """Same-direction sum of min weights: longs with longs, shorts with shorts."""
    tot = 0.0
    for s in set(a) & set(b):
        x, y = a[s], b[s]
        if x > 0 and y > 0:
            tot += min(x, y)
        elif x < 0 and y < 0:
            tot += min(-x, -y)
    return tot


def ai_targets_by_day(conn: sqlite3.Connection) -> dict[str, dict[str, dict[str, float]]]:
    """snapshot_date -> AI agent -> its target weights after risk (holds carry the last targets)."""
    ai = [a for a in AGENT_MODELS if not a.startswith("C")]
    snaps = conn.execute("""SELECT snapshot_id, snapshot_date FROM snapshots
                            WHERE asset_class = 'us_equity' AND corrects_id IS NULL ORDER BY snapshot_date""").fetchall()
    last: dict[str, dict[str, float]] = {}
    out: dict[str, dict[str, dict[str, float]]] = {}
    names = ", ".join("?" * len(ai))
    for snap in snaps:
        for row in conn.execute(
                f"""SELECT * FROM decisions AS d WHERE d.snapshot_id = ? AND d.agent_id IN ({names})
                      AND d.corrects_id IS NULL
                      AND d.decision_id = (SELECT q.decision_id FROM decisions AS q
                                           WHERE q.agent_id = d.agent_id AND q.snapshot_id = d.snapshot_id
                                             AND q.corrects_id IS NULL
                                           ORDER BY q.attempt DESC, q.decision_id DESC LIMIT 1)""",
                [snap["snapshot_id"], *ai]):
            w = applied_weights(conn, row)
            if w is not None:
                last[row["agent_id"]] = {s: x for s, x in w.items() if x}
        out[snap["snapshot_date"]] = {a: dict(w) for a, w in last.items()}
    return out


def overlap_by_day(conn: sqlite3.Connection) -> dict[str, dict[tuple[str, str], float]]:
    """snapshot_date -> (agent a, agent b) -> overlap, for every pair that both have targets."""
    out = {}
    for day, targets in ai_targets_by_day(conn).items():
        ids = sorted(targets, key=_order)
        out[day] = {(a, b): overlap(targets[a], targets[b]) for a, b in combinations(ids, 2)}
    return out


def overlap_report(conn: sqlite3.Connection) -> list[str]:
    days = {d: p for d, p in overlap_by_day(conn).items() if p}
    lines = ["", "AI target overlap (same-direction sum of min weights; 0 = none, 1 = same book)"]
    if not days:
        return lines + ["  no AI decisions yet"]
    lines.append(f"  {'day':<10} {'pairs':>5} {'mean':>6} {'min':>6} {'max':>6}  most similar")
    for d, pairs in days.items():
        v = list(pairs.values())
        top = max(pairs, key=lambda k: (pairs[k], k))
        lines.append(f"  {d:<10} {len(v):>5} {mean(v):>6.2f} {min(v):>6.2f} {max(v):>6.2f}  "
                     f"{top[0]}-{top[1]} {pairs[top]:.2f}")
    per_pair: dict[tuple[str, str], list[float]] = {}
    for pairs in days.values():
        for k, x in pairs.items():
            per_pair.setdefault(k, []).append(x)
    lines.append("  pair means over the days: " + ", ".join(
        f"{a}-{b} {mean(v):.2f}" for (a, b), v in sorted(per_pair.items(), key=lambda kv: (_order(kv[0][0]), _order(kv[0][1])))))
    return lines


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
    lines += overlap_report(conn)
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
