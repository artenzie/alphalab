"""Smoke run of the crypto side agents C0 and C1 (protocol §13, §7 rule 4 v1.12) on a temporary copy of the database.

    venv/bin/python scripts/backtest_crypto.py               # decision days 7..16 Nov 2026
    venv/bin/python scripts/backtest_crypto.py --days 10 --last 2026-11-16

The real database is opened read-only and copied (sqlite backup) to a temp
file; every write (snapshots, decisions, risk events, orders, fills, positions,
equity) goes to the copy, which is deleted afterwards. Snapshots are backtest
reconstructions; decisions are stamped at the §13 decide time (00:15Z on D+1).

Each crypto day D, per agent: settle_day(D) fills D-1's orders at the first
1-minute bar at or after D-1's decision time (crypto_fill_bars) and marks at
D's daily close; then the agent decides on D's snapshot, the §6/§13 risk
checks run, and place_orders writes the orders. The first day is day 1; the
last day's orders are never filled.

C1 is a real AIAgent on agent 8's Claude config through OpenRouter: about one
call per day (a few cents). The key is read from .env and never printed.

Checks:
  1. C0 matches its formula: with s BTC filled at O (the minute-bar open after
     day 1's decision), final equity = V0 - s*O*(1+5bps)*(1+0.25%) + s*P_end.
  2. Cash never below 0 (cash floor, both agents).
  3. Accounting identity every day: change in equity = price P&L - slippage - fees.
  4. After every non-hold C1 decision, no coin is above 50% at that decision's close
     (plus at most a sub-$100 trim skipped by rule 2). Hold days may drift above 50%
     (§6: risk checks run on decisions); that drift is reported, not checked.
  5. Every fill is at a minute bar starting at or after its order's created_at,
     and it is the first such bar stored.
  6. The real database is unchanged.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import tempfile
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents.harness import AIAgent  # noqa: E402
from agents.templates import load as load_template  # noqa: E402
from agents.models import model_for  # noqa: E402
from data import calendar as cal  # noqa: E402
from data import db  # noqa: E402
from data.snapshot import build_snapshot  # noqa: E402
from execution import ledger as L  # noqa: E402
from execution import ledger_db as LD  # noqa: E402
from execution.risk import TOL, apply_risk  # noqa: E402
from scripts.backtest_ledger import copy_db, store_snapshot_row  # noqa: E402
from strategies import registry  # noqa: E402
from strategies.base import AgentMemory, PastDecision, Portfolio, build_context  # noqa: E402

AGENT_IDS = ("C0", "C1")
NAMES = {"C0": "Bitcoin benchmark", "C1": "AI crypto trader (Claude)"}
BTC = "BTC/USD"
TABLES = ("prices", "crypto_fill_bars", "snapshots", "decisions", "orders", "fills", "positions", "equity",
          "risk_events", "heartbeat")


def counts(path: Path) -> dict[str, int]:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    finally:
        c.close()


def insert(conn, table: str, row: dict) -> int:
    cols = ", ".join(row)
    return conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(row))})",
                        list(row.values())).lastrowid


def make_agents(client):
    c1 = AIAgent("C1", model_for("C1"), load_template("C1"), client, sleep=lambda s: None, backoff=())
    return {"C0": registry.get_agent("C0"), "C1": c1}


def run(db_path: Path, days: list[date], client) -> dict:
    before = counts(db_path)
    now = datetime.now(timezone.utc)
    report: dict = {"days": days, "agents": {}}
    with tempfile.TemporaryDirectory() as tmp:
        conn = copy_db(db_path, Path(tmp) / "crypto.db")
        try:
            snaps = {}
            for d in days:
                snap = build_snapshot(conn, "crypto", d, "backtest", now=now)
                snaps[d] = (snap, store_snapshot_row(conn, snap))
            agents = make_agents(client)
            for aid in AGENT_IDS:
                agent, rows, past = agents[aid], [], []
                for i, d in enumerate(days):
                    res = LD.settle_day(conn, aid, d, now, mode="backtest")
                    snap, sid = snaps[d]
                    closes = {s: f["last_close"] for s, f in snap.payload["symbols"].items()}
                    acct = LD.load_account(conn, aid, as_of=d)
                    pv = acct.value(closes)
                    portfolio = Portfolio(cash=acct.cash, positions={s: p.qty for s, p in acct.positions.items()},
                                          value=pv)
                    ctx = build_context(d, days[:i + 1])
                    if aid == "C1":
                        ctx = replace(ctx, memory=AgentMemory(
                            starting_value=L.START_CASH, avg_cost={s: p.avg_cost for s, p in acct.positions.items()},
                            last_decisions=tuple(past[-5:]), end_date=days[-1],
                            trading_days_left=len(days) - i - 1))
                    dec = agent(snap, portfolio, ctx)
                    current = {s: p.qty * closes[s] / pv for s, p in acct.positions.items()}
                    weights, events = apply_risk(aid, dec, {}, current)
                    decided_at = cal.crypto_times(d).decide_at
                    record = getattr(dec, "record", None)
                    with conn:
                        if record is None:
                            did = insert(conn, "decisions", {
                                "agent_id": aid, "snapshot_id": sid, "decided_at": db.utc_ts(decided_at),
                                "status": "ok", "parsed_targets": json.dumps(None if dec.weights is None
                                                                             else dict(dec.weights)),
                                "overall_reasoning": dec.reason})
                        else:
                            for a in record.attempts:
                                did = insert(conn, "decisions", {"agent_id": aid, "snapshot_id": sid,
                                                                 "decided_at": db.utc_ts(decided_at), **a.row()})
                        LD.write_risk_events(conn, aid, events, decided_at, did)
                    placed = LD.place_orders(conn, aid, d, closes, weights, decided_at, did)
                    if record is not None:
                        last = record.attempts[-1]
                        p = last.parsed
                        past.append(PastDecision(d, record.status, None if p is None or p.targets is None
                                                 else dict(p.targets),
                                                 {} if p is None or p.reasons is None else dict(p.reasons),
                                                 dec.reason if p is None else p.reasoning))
                    rows.append({"date": d, "mark": res.mark, "fills": res.fills, "floor": res.floor_events,
                                 "closes": closes, "asked": None if dec.weights is None else dict(dec.weights),
                                 "applied": weights, "events": events, "orders": placed.orders, "reason": dec.reason,
                                 "record": record, "acct_before": acct, "pv": pv})
                report["agents"][aid] = rows
            report["checks"] = checks(conn, report, days)
            report["btc"] = {d: snaps[d][0].payload["symbols"][BTC]["last_close"] for d in days}
        finally:
            conn.close()
    report["real_db_unchanged"] = counts(db_path) == before
    return report


def checks(conn, report: dict, days: list[date]) -> dict:
    out = {}
    # 1. C0 formula
    c0 = report["agents"]["C0"]
    btc_fills = [f for r in c0 for f in r["fills"] if f.symbol == BTC]
    s = sum(f.qty for f in btc_fills)
    o = btc_fills[0].reference_price if btc_fills else float("nan")
    p_end = c0[-1]["closes"][BTC]
    predicted = L.START_CASH - s * o * (1 + L.SLIPPAGE_BPS / 1e4) * (1 + L.CRYPTO_FEE) + s * p_end
    out["c0_formula"] = {"shares": s, "open": o, "p_end": p_end, "predicted": predicted,
                         "actual": c0[-1]["mark"].equity, "diff": c0[-1]["mark"].equity - predicted,
                         "ok": len(btc_fills) == 1 and abs(c0[-1]["mark"].equity - predicted) < 0.01}
    # 2. cash floor
    min_cash = {aid: min(r["mark"].cash for r in rows) for aid, rows in report["agents"].items()}
    out["cash"] = {"min": min_cash, "ok": all(v >= -1e-9 for v in min_cash.values())}
    # 3. accounting identity
    worst = 0.0
    for aid, rows in report["agents"].items():
        for prev, cur in zip(rows, rows[1:]):
            held = LD.load_account(conn, aid, as_of=prev["date"]).positions
            c_prev, c_cur = prev["closes"], cur["closes"]
            pnl = math.fsum(p.qty * (c_cur[s] - c_prev[s]) for s, p in held.items())
            pnl += math.fsum(f.signed_qty * (c_cur[f.symbol] - f.reference_price) for f in cur["fills"])
            cost = math.fsum(f.slippage_usd + f.fee_usd for f in cur["fills"])
            diff = (cur["mark"].equity - prev["mark"].equity) - (pnl - cost)
            worst = max(worst, abs(diff))
    out["identity"] = {"worst_diff": worst, "ok": worst < 1e-6}
    # 4. C1 cap at the decision close, on non-hold decisions (risk checks run on decisions only;
    #    a hold lets weights drift, and rule 2 may leave a sub-$100 trim untraded)
    worst_w, worst_slack, drift = 0.0, 0.0, 0.0
    for r in report["agents"]["C1"]:
        shares = {s: r["acct_before"].shares(s) for s in r["closes"]}
        for o in r["orders"]:
            shares[o.symbol] = shares.get(o.symbol, 0.0) + o.signed_qty
        w = max([0.0] + [q * r["closes"][s] / r["pv"] for s, q in shares.items()])
        if r["applied"] is None:
            drift = max(drift, w)
        else:
            worst_w = max(worst_w, w)
            worst_slack = max(worst_slack, w - (0.5 + L.MIN_TRADE_USD / r["pv"]))
    out["c1_cap"] = {"max_weight": worst_w, "max_hold_drift": drift, "ok": worst_slack <= TOL}
    # 5. fill bars
    bad = []
    for f in conn.execute("""SELECT f.filled_at, f.reference_price, f.detail, o.created_at, o.symbol
                             FROM fills f JOIN orders o ON o.order_id = f.order_id WHERE f.status = 'filled'"""):
        b = conn.execute("SELECT * FROM crypto_fill_bars WHERE fill_bar_id = ?",
                         (json.loads(f["detail"])["fill_bar_id"],)).fetchone()
        t_ref = db.utc_ts(LD.ceil_minute(cal._parse_ts(f["created_at"])))
        first = conn.execute("SELECT MIN(bar_ts) FROM crypto_fill_bars WHERE symbol = ? AND bar_ts >= ?",
                             (f["symbol"], t_ref)).fetchone()[0]
        if not (b["bar_ts"] == f["filled_at"] == first and f["filled_at"] >= f["created_at"]
                and b["open"] == f["reference_price"]):
            bad.append(dict(f))
    n_fills = conn.execute("SELECT COUNT(*) FROM fills WHERE status = 'filled'").fetchone()[0]
    out["fill_bars"] = {"fills": n_fills, "bad": bad, "ok": not bad}
    return out


def fmt_w(w: dict | None) -> str:
    if w is None:
        return "hold"
    shown = {s.split("/")[0]: x for s, x in sorted(w.items(), key=lambda kv: -kv[1]) if x}
    return ", ".join(f"{s} {x:.0%}" for s, x in shown.items()) or "all cash"


def format_report(r: dict) -> str:
    days = r["days"]
    c0, c1 = r["agents"]["C0"], r["agents"]["C1"]
    btc0 = r["btc"][days[0]]
    lines = [f"{len(days)} crypto days {days[0]} .. {days[-1]} (backtest snapshots, temp DB copy; "
             f"decide 00:15Z, fill at the first 1-min bar after the decision)", "",
             "Equity paths (mark at each daily close)",
             f"{'day':<11} {'C0 equity':>12} {'C0 ret':>8} {'C1 equity':>12} {'C1 ret':>8} {'BTC close':>11} {'BTC ret':>8}"]
    for a, b in zip(c0, c1):
        d = a["date"]
        lines.append(f"{d}  {a['mark'].equity:>12,.2f} {a['mark'].equity / L.START_CASH - 1:>+8.2%} "
                     f"{b['mark'].equity:>12,.2f} {b['mark'].equity / L.START_CASH - 1:>+8.2%} "
                     f"{r['btc'][d]:>11,.2f} {r['btc'][d] / btc0 - 1:>+8.2%}")
    lines.append("")
    for aid in AGENT_IDS:
        rows = r["agents"][aid]
        fills = [f for x in rows for f in x["fills"]]
        lines.append(f"{aid} · {NAMES[aid]}: {len(fills)} fills, fees ${sum(f.fee_usd for f in fills):,.2f}, "
                     f"slippage ${sum(f.slippage_usd for f in fills):,.2f}, cash_floor events "
                     f"{sum(len(x['floor']) for x in rows)}, final cash ${rows[-1]['mark'].cash:,.6f}")
    lines += ["", "C1 choices (decision on D at 00:15Z D+1; asked -> after risk checks)"]
    total_cost = 0.0
    for x in c1:
        rec = x["record"]
        a = rec.attempts[-1]
        cost = sum(t.cost_usd or 0.0 for t in rec.attempts)
        total_cost += cost
        clips = ", ".join(f"{e['rule']} {e['symbol'].split('/')[0]}" for e in x["events"])
        conf = a.parsed.confidence if a.parsed is not None else None
        lines.append(f"  {x['date']}  {rec.status:<7} ({len(rec.attempts)} call{'s' if len(rec.attempts) > 1 else ''}, "
                     f"in {a.tokens_in} out {a.tokens_out} reasoning {a.reasoning_tokens}, ${cost:.4f})"
                     f"{'' if conf is None else f'  confidence {conf}'}")
        lines.append(f"      asked: {fmt_w(x['asked'])}")
        if x["asked"] is not None:
            lines.append(f"      after risk: {fmt_w(x['applied'])}" + (f"   [clips: {clips}]" if clips else ""))
        lines.append(f"      orders: " + (", ".join(f"{o.side} {o.qty:.6g} {o.symbol.split('/')[0]}"
                                               for o in x["orders"]) or "none"))
        lines.append(f"      reason: {x['reason'][:220]}")
        for t in rec.attempts:
            if t.error:
                lines.append(f"      attempt {t.attempt} {t.status}: {t.error[:200]}")
    c = r["checks"]
    f = c["c0_formula"]
    lines += ["", f"C1 total OpenRouter cost: ${total_cost:.4f}", "",
              "Checks",
              f"  1 C0 formula: {f['shares']:.9f} BTC at open {f['open']:,.3f}, last close {f['p_end']:,.2f}: "
              f"predicted {f['predicted']:,.4f}, actual {f['actual']:,.4f}, diff ${f['diff']:+.6f}  "
              f"{'OK' if f['ok'] else 'FAILED'}",
              f"  2 cash floor: min cash C0 {c['cash']['min']['C0']:.6f}, C1 {c['cash']['min']['C1']:.6f}  "
              f"{'OK' if c['cash']['ok'] else 'FAILED'}",
              f"  3 accounting identity: worst diff ${c['identity']['worst_diff']:.10f}  "
              f"{'OK' if c['identity']['ok'] else 'FAILED'}",
              f"  4 C1 max weight per coin after a non-hold decision, at its close: {c['c1_cap']['max_weight']:.6f} "
              f"(limit 0.5 + $100/value; on hold days it drifted to {c['c1_cap']['max_hold_drift']:.6f}, "
              f"not checked by design)  "
              f"{'OK' if c['c1_cap']['ok'] else 'FAILED'}",
              f"  5 fills at the first minute bar at/after their order: {c['fill_bars']['fills']} fills, "
              f"{len(c['fill_bars']['bad'])} bad  {'OK' if c['fill_bars']['ok'] else 'FAILED'}",
              f"  6 real database unchanged: {r['real_db_unchanged']}  {'OK' if r['real_db_unchanged'] else 'FAILED'}"]
    return "\n".join(lines)


def all_ok(r: dict) -> bool:
    return all(v["ok"] for v in r["checks"].values()) and r["real_db_unchanged"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--days", type=int, default=10)
    p.add_argument("--last", type=date.fromisoformat, default=date(2026, 11, 16), metavar="YYYY-MM-DD")
    p.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    args = p.parse_args(argv)
    load_dotenv(ROOT / ".env")
    from agents.openrouter import OpenRouterClient
    days = [args.last - timedelta(days=args.days - 1 - i) for i in range(args.days)]
    r = run(args.db, days, OpenRouterClient())
    print(format_report(r))
    ok = all_ok(r)
    print("\nCHECK", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
