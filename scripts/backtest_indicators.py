"""Agents 3-6 over the last N real sessions, on backtest snapshots: how often do they trade?

    venv/bin/python scripts/backtest_indicators.py            # last 57 sessions
    venv/bin/python scripts/backtest_indicators.py --sessions 120

There are no 2027 prices yet, so this stands in for the 57-session run: the
last N sessions in the database, the first one treated as day 1, with week
flags from build_context. The database is opened read-only. Snapshots are
backtest reconstructions (see data/snapshot.py), not point-in-time views.

The portfolio is simulated to count trades, not to measure performance:
$100,000, each decision filled at that day's close with no costs, slippage,
borrow or risk limits. An "order" is one symbol whose share count changes:
an open/close (to or from zero, or a flip) or a resize of a position kept on the
same side (back to its target weight after drift). Gross is sum |weight|.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data import calendar as cal  # noqa: E402
from data.snapshot import build_snapshot  # noqa: E402
from strategies import registry  # noqa: E402
from strategies.base import Portfolio, build_context  # noqa: E402

AGENT_IDS = (3, 4, 5, 6)
START_VALUE = 100_000.0
EPS = 1e-9


def no_network():
    raise RuntimeError("calendar not cached; this script never calls Alpaca")


def last_sessions(conn: sqlite3.Connection, n: int) -> list[date]:
    last = date.fromisoformat(conn.execute("SELECT max(bar_date) FROM prices WHERE asset_class='us_equity'")
                              .fetchone()[0])
    out, d = [], last
    while len(out) < n:
        if cal.is_trading_day(d, conn, trading=no_network):
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def run(db_path: Path, n_sessions: int = 57) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sessions = last_sessions(conn, n_sessions)
        snaps = [build_snapshot(conn, "us_equity", d, "backtest", trading=no_network) for d in sessions]
    finally:
        conn.close()

    report = {"sessions": sessions, "weeks": 0, "agents": {}}
    contexts = [build_context(d, sessions[:i + 1]) for i, d in enumerate(sessions)]
    report["weeks"] = sum(c.is_first_session_of_week for c in contexts)
    for agent_id in AGENT_IDS:
        agent = registry.get_agent(agent_id)
        cash, shares = START_VALUE, {}
        r = {"name": registry.NAMES[agent_id], "decisions": 0, "orders": 0, "opens_closes": 0, "resizes": 0,
             "targets": [], "positions": [], "max_gross": 0.0}
        for snap, c in zip(snaps, contexts):
            px = {s: f["last_close"] for s, f in snap.payload["symbols"].items()}
            value = cash + sum(q * px[s] for s, q in shares.items())
            dec = agent(snap, Portfolio(cash=cash, positions=shares, value=value), c)
            if not dec.is_hold:
                r["decisions"] += 1
                r["targets"].append(dict(dec.weights))
                r["max_gross"] = max(r["max_gross"], sum(abs(w) for w in dec.weights.values()))
                new = {s: w * value / px[s] for s, w in dec.weights.items() if w != 0}
                for s in set(shares) | set(new):
                    old_q, new_q = shares.get(s, 0.0), new.get(s, 0.0)
                    if abs(new_q - old_q) > EPS:
                        r["orders"] += 1
                        r["resizes" if old_q * new_q > 0 else "opens_closes"] += 1
                shares = new
                cash = value - sum(q * px[s] for s, q in shares.items())
            r["positions"].append(len(shares))
        report["agents"][agent_id] = r
    return report


def format_report(report: dict) -> str:
    s = report["sessions"]
    lines = [f"{len(s)} sessions {s[0]} .. {s[-1]} ({report['weeks']} week starts), backtest snapshots",
             f"{'#':>2}  {'agent':<22} {'decisions':>9} {'orders':>7} {'open/close':>10} {'resize':>7} "
             f"{'avg pos':>8} {'max pos':>8} {'max gross':>9}"]
    for agent_id, r in report["agents"].items():
        pos = r["positions"]
        lines.append(f"{agent_id:>2}  {r['name']:<22} {r['decisions']:>9} {r['orders']:>7} {r['opens_closes']:>10} "
                     f"{r['resizes']:>7} {sum(pos) / len(pos):>8.1f} {max(pos):>8} {r['max_gross']:>9.0%}")
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
