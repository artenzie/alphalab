"""Agents 3-6 decisions from backtest_indicators through the §6 risk checks: how often are they clipped?

    venv/bin/python scripts/backtest_risk.py               # last 57 sessions
    venv/bin/python scripts/backtest_risk.py --sessions 120

Each non-hold target from scripts/backtest_indicators.py goes through
execution.risk.apply_risk, with the weights held just before the decision as
current_weights. Two limits on what this shows:
  - there is no history of Alpaca's easy-to-borrow flags, so every stock is
    treated as easy-to-borrow;
  - the simulated portfolio follows the unclipped targets, so this counts the
    clips each decision would get; it does not re-simulate the clipped path.
Hold days are not checked (holds pass through), and margin is not modelled.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from execution.risk import apply_risk  # noqa: E402
from scripts.backtest_indicators import run as run_indicators  # noqa: E402
from strategies.base import Decision  # noqa: E402


def run(db_path: Path, n_sessions: int = 57) -> dict:
    base = run_indicators(db_path, n_sessions)
    out = {"sessions": base["sessions"], "agents": {}}
    for agent_id, r in base["agents"].items():
        res = {"name": r["name"], "decisions": len(r["targets"]), "clipped": 0, "events": 0,
               "rules": Counter(), "max_cut": 0.0}
        for target, current in zip(r["targets"], r["current"]):
            etb = {s: True for s in target}
            clipped, events = apply_risk(agent_id, Decision(target, "backtest"), etb, current)
            if events:
                res["clipped"] += 1
                res["events"] += len(events)
                res["rules"].update(e["rule"] for e in events)
                res["max_cut"] = max(res["max_cut"], max(abs(e["requested_weight"]) - abs(e["applied_weight"])
                                                        for e in events))
        out["agents"][agent_id] = res
    return out


def format_report(report: dict) -> str:
    s = report["sessions"]
    lines = [f"{len(s)} sessions {s[0]} .. {s[-1]}, backtest snapshots, all stocks treated as easy-to-borrow",
             f"{'#':>2}  {'agent':<22} {'decisions':>9} {'clipped':>8} {'events':>7} {'max cut':>8}  rules"]
    for agent_id, r in report["agents"].items():
        rules = ", ".join(f"{k} {v}" for k, v in sorted(r["rules"].items())) or "-"
        lines.append(f"{agent_id:>2}  {r['name']:<22} {r['decisions']:>9} {r['clipped']:>8} {r['events']:>7} "
                     f"{r['max_cut']:>8.2%}  {rules}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", type=Path, default=ROOT / "data" / "alphalab.db")
    p.add_argument("--sessions", type=int, default=57)
    a = p.parse_args(argv)
    print(format_report(run(a.db, a.sessions)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
