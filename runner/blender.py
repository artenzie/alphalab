"""Agent 13's inputs from the experiment database (protocol §4, §5, v1.16).

    marks = marks_up_to(conn, "3", day)          # agent 3's equity marks, dated <= day, oldest first
    stats = strategy_stats(marks)                # ret_5, ret_21, return_since_start, vol_20, max_drawdown
    alloc = current_allocation(conn, day)        # agent 13's allocation before day, or None

Rule 1: only marks with as_of_date <= the decision date are read (the newest row per
day, so corrections apply, as in analysis/results.py). A day's mark is written at its
close, by the settle step that runs before the snapshot; nothing later can enter.

A figure is None when there are too few marks:
  return_since_start  1 mark (equity / START_CASH - 1)
  ret_5, ret_21       6 and 22 marks (equity now / equity 5 or 21 marks earlier - 1)
  vol_20              21 marks: sample std (ddof=1) of the last 20 daily returns x sqrt(252),
                      as the snapshot's vol_20
  max_drawdown        2 marks: the largest fall from a running peak, a fraction >= 0
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date
from typing import Sequence

from agents.harness import BLEND_OF, BLENDER_ID
from execution import ledger as L

ANNUAL = 252


def marks_up_to(conn: sqlite3.Connection, agent_id: str, day: date) -> list[float]:
    """The agent's equity at each close dated on or before `day`, oldest first (newest row per day)."""
    rows = conn.execute(
        """SELECT e.as_of_date, e.equity FROM equity AS e
           WHERE e.agent_id = :a AND e.as_of_date <= :d
             AND e.equity_id = (SELECT MAX(q.equity_id) FROM equity AS q
                                WHERE q.agent_id = e.agent_id AND q.as_of_date = e.as_of_date)
           ORDER BY e.as_of_date""", {"a": agent_id, "d": day.isoformat()}).fetchall()
    return [float(r[1]) for r in rows]


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 6)


def strategy_stats(marks: Sequence[float], start_cash: float = L.START_CASH) -> dict[str, float | None]:
    n = len(marks)

    def ret(k: int) -> float | None:
        return marks[-1] / marks[-1 - k] - 1 if n > k else None

    vol = None
    if n >= 21:
        last = marks[-21:]
        rets = [last[i] / last[i - 1] - 1 for i in range(1, 21)]
        m = math.fsum(rets) / 20
        vol = math.sqrt(math.fsum((x - m) ** 2 for x in rets) / 19) * math.sqrt(ANNUAL)
    dd = None
    if n >= 2:
        peak, dd = marks[0], 0.0
        for x in marks:
            peak = max(peak, x)
            dd = max(dd, 1 - x / peak)
    return {"ret_5": _r(ret(5)), "ret_21": _r(ret(21)),
            "return_since_start": _r(marks[-1] / start_cash - 1) if n else None,
            "vol_20": _r(vol), "max_drawdown": _r(dd)}


def current_allocation(conn: sqlite3.Connection, day: date) -> dict[str, float] | None:
    """Agent 13's allocation from its latest valid row before `day` (an allocation or a follow row)."""
    row = conn.execute(
        """SELECT d.parsed_allocation FROM decisions AS d JOIN snapshots AS s ON s.snapshot_id = d.snapshot_id
           WHERE d.agent_id = :a AND d.status = 'ok' AND d.parsed_allocation IS NOT NULL
             AND d.corrects_id IS NULL AND s.asset_class = 'us_equity' AND s.snapshot_date < :d
           ORDER BY s.snapshot_date DESC, d.attempt DESC, d.decision_id DESC LIMIT 1""",
        {"a": BLENDER_ID, "d": day.isoformat()}).fetchone()
    if row is None:
        return None
    alloc = json.loads(row[0])
    return {k: float(alloc[k]) for k in BLEND_OF}
