"""Smoke run of the ledger: agents 1 and 2 over the last N real sessions, on a temporary copy of the database.

    venv/bin/python scripts/backtest_ledger.py               # last 20 sessions
    venv/bin/python scripts/backtest_ledger.py --sessions 57

The real database is opened read-only and copied (sqlite backup) to a temp
file; every write (snapshots, decisions, risk events, orders, fills,
positions, equity) goes to the copy, which is deleted afterwards. Snapshots
are backtest reconstructions and the ledger runs in backtest mode
(split-adjusted prices); every stock is treated as easy-to-borrow.

Each session D, per agent: settle_day(D) fills D-1's orders at D's open,
accrues and marks at D's close; then the agent decides on D's snapshot, the
§6 risk checks run, and place_orders writes the orders for D+1. The first
session is day 1. The last session's orders are never filled.

Check for agent 1. It is sized at the day-1 close C0 and filled at the day-2
open O (plus 5 bps), so with s = V0 / C0 shares its final equity is exactly
    V0 - s * O * 1.0005 + s * P_end - interest - borrow
i.e. return = (O / C0) * [(P_end / O - 1) - 0.0005] - interest / V0: SPY's
open-to-end return minus one slippage charge, scaled by O / C0, less a few
dollars of margin interest when O > C0 leaves cash slightly negative.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data import calendar as cal  # noqa: E402
from data import db  # noqa: E402
from data.snapshot import build_snapshot  # noqa: E402
from execution import ledger as L  # noqa: E402
from execution import ledger_db as LD  # noqa: E402
from execution.risk import apply_risk  # noqa: E402
from scripts.backtest_indicators import last_sessions, no_network  # noqa: E402
from strategies import registry  # noqa: E402
from strategies.base import Portfolio, build_context  # noqa: E402

AGENT_IDS = (1, 2)


def copy_db(src: Path, dst: Path) -> sqlite3.Connection:
    ro = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    out = sqlite3.connect(str(dst))
    try:
        ro.backup(out)
    finally:
        ro.close()
        out.close()
    return db.connect(dst)


def counts(path: Path) -> dict[str, int]:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("orders", "fills", "positions", "equity", "decisions", "risk_events")}
    finally:
        c.close()


def store_snapshot_row(conn, snap) -> int:
    """The backtest snapshot in the copy only (store_snapshot refuses backtest snapshots by design)."""
    with conn:
        return conn.execute(
            "INSERT INTO snapshots (asset_class, snapshot_date, cutoff_ts, built_at, payload, sha256, checks, "
            "checks_passed) VALUES (?, ?, ?, ?, ?, ?, '[]', 1)",
            (snap.asset_class, snap.decision_date, snap.cutoff_ts, snap.cutoff_ts, snap.canonical,
             snap.sha256)).lastrowid


def run(db_path: Path, n_sessions: int = 20) -> dict:
    before = counts(db_path)
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        conn = copy_db(db_path, Path(tmp) / "backtest.db")
        try:
            sessions = last_sessions(conn, n_sessions)
            report = {"sessions": sessions, "agents": {}}
            snaps = {}
            for d in sessions:
                snap = build_snapshot(conn, "us_equity", d, "backtest", trading=no_network)
                snaps[d] = (snap, store_snapshot_row(conn, snap))
            for agent_id in AGENT_IDS:
                agent, aid = registry.get_agent(agent_id), str(agent_id)
                days = []
                for i, d in enumerate(sessions):
                    res = LD.settle_day(conn, aid, d, now, mode="backtest", trading=no_network)
                    snap, sid = snaps[d]
                    closes = {s: f["last_close"] for s, f in snap.payload["symbols"].items()}
                    acct = LD.load_account(conn, aid, as_of=d)
                    pv = acct.value(closes)
                    portfolio = Portfolio(cash=acct.cash, positions={s: p.qty for s, p in acct.positions.items()},
                                          value=pv)
                    dec = agent(snap, portfolio, build_context(d, sessions[:i + 1]))
                    current = {s: p.qty * closes[s] / pv for s, p in acct.positions.items()}
                    weights, events = apply_risk(agent_id, dec, {s: True for s in closes}, current)
                    decided_at = cal.equity_times(d, conn, no_network).decide_at
                    with conn:
                        did = conn.execute(
                            "INSERT INTO decisions (agent_id, snapshot_id, decided_at, status, parsed_targets, "
                            "overall_reasoning) VALUES (?, ?, ?, 'ok', ?, ?)",
                            (aid, sid, db.utc_ts(decided_at), json.dumps(weights), dec.reason)).lastrowid
                        LD.write_risk_events(conn, aid, events, decided_at, did)
                    placed = LD.place_orders(conn, aid, d, closes, weights, decided_at, did)
                    days.append({"date": d, "equity": res.mark.equity, "fills": res.fills, "borrow": res.borrow,
                                 "interest": res.interest, "orders": placed.orders, "clips": len(events),
                                 "margin": len(placed.margin_events)})
                report["agents"][agent_id] = {"name": registry.NAMES[agent_id], "days": days}
            report["spy"] = spy_check(conn, sessions, report["agents"][1]["days"])
        finally:
            conn.close()
    report["real_db_unchanged"] = counts(db_path) == before
    return report


def spy_check(conn, sessions, days) -> dict:
    bars = {d: LD.day_bars(conn, ["SPY"], d, datetime.now(timezone.utc))["SPY"] for d in sessions[:2] + sessions[-1:]}
    c0, o, p_end = bars[sessions[0]]["adj_close"], bars[sessions[1]]["adj_open"], bars[sessions[-1]]["adj_close"]
    v0, v_end = days[0]["equity"], days[-1]["equity"]
    interest = sum(x["interest"] for x in days)
    borrow = sum(x["borrow"] for x in days)
    s = L.round_long(v0 / c0)
    predicted_end = v0 - s * o * (1 + L.SLIPPAGE_BPS / 1e4) + s * p_end - interest - borrow
    return {"c0": c0, "open": o, "p_end": p_end, "spy_open_to_end": p_end / o - 1,
            "spy_close_to_end": p_end / c0 - 1, "agent_return": v_end / v0 - 1,
            "formula_return": (o / c0) * ((p_end / o - 1) - L.SLIPPAGE_BPS / 1e4) - interest / v0,
            "slippage_usd": s * o * L.SLIPPAGE_BPS / 1e4, "interest": interest,
            "predicted_end": predicted_end, "agent_end": v_end, "diff_usd": v_end - predicted_end}


def format_report(r: dict) -> str:
    s = r["sessions"]
    lines = [f"{len(s)} sessions {s[0]} .. {s[-1]} (backtest snapshots, ledger in backtest mode, temp DB copy)", ""]
    for agent_id, a in r["agents"].items():
        days = a["days"]
        fills = [f for d in days for f in d["fills"]]
        lines.append(f"Agent {agent_id} · {a['name']}")
        lines.append(f"  equity {days[0]['equity']:,.2f} -> {days[-1]['equity']:,.2f}  "
                     f"return {days[-1]['equity'] / days[0]['equity'] - 1:+.4%}")
        lines.append(f"  fills {len(fills)}  slippage ${sum(f.slippage_usd for f in fills):,.2f}  "
                     f"interest ${sum(d['interest'] for d in days):,.2f}  borrow ${sum(d['borrow'] for d in days):,.2f}  "
                     f"clips {sum(d['clips'] for d in days)}  margin events {sum(d['margin'] for d in days)}")
        for d in days:
            if d["fills"]:
                buys = sum(f.qty * f.fill_price for f in d["fills"] if f.side == "buy")
                sells = sum(f.qty * f.fill_price for f in d["fills"] if f.side == "sell")
                lines.append(f"    {d['date']}: {len(d['fills'])} fills, bought ${buys:,.2f}, sold ${sells:,.2f}")
        lines.append("")
    c = r["spy"]
    lines += [
        "Agent 1 vs SPY",
        f"  SPY day-1 close {c['c0']:.4f}, day-2 open {c['open']:.4f}, last close {c['p_end']:.4f}",
        f"  SPY return, day-2 open -> last close   {c['spy_open_to_end']:+.6%}",
        f"  SPY return, day-1 close -> last close  {c['spy_close_to_end']:+.6%}",
        f"  Agent 1 return                         {c['agent_return']:+.6%}",
        f"  (O/C0)*[SPY open->end - 5 bps] - interest/V0 = {c['formula_return']:+.6%}",
        f"  one slippage charge ${c['slippage_usd']:,.2f}; margin interest ${c['interest']:,.2f}",
        f"  agent 1 final equity {c['agent_end']:,.4f} vs formula {c['predicted_end']:,.4f}: "
        f"diff ${c['diff_usd']:+.6f}",
        "",
        f"Real database unchanged (orders/fills/positions/equity/decisions/risk_events counts): "
        f"{r['real_db_unchanged']}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--sessions", type=int, default=20)
    p.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    args = p.parse_args(argv)
    r = run(args.db, args.sessions)
    print(format_report(r))
    ok = abs(r["spy"]["diff_usd"]) < 0.01 and r["real_db_unchanged"]
    print("\nCHECK", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
