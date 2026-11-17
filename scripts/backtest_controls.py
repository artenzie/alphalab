"""Backtest of the six rule-based controls (agents 1-6) through the real ledger and risk checks.

    venv/bin/python scripts/backtest_controls.py                       # full run, writes analysis/backtests/<today>/
    venv/bin/python scripts/backtest_controls.py --sessions 30 --no-out  # quick look at the first 30 sessions

A BACKTEST WITH TODAY'S DATA, NOT A FORECAST. Snapshots are backtest
reconstructions (data/snapshot.py: the latest stored bars and today's split
adjustment, not what was known on each day). The official results come only
from the live run that starts on Mon 11 Jan 2027.

Sessions: day 1 is the first session on which every universe symbol has at
least 100 stored bars (so ma_100 exists); the run ends at --end (13 Nov 2026).
Each session D, per agent: settle_day(D) fills D-1's orders at D's open,
accrues and marks at D's close; then the agent decides on D's snapshot, the §6
risk checks run (every stock treated as easy-to-borrow), and place_orders
writes the orders for D+1. The last session's orders are never filled.

The real database is opened read-only and copied (sqlite backup) to a temp
file; every write goes to the copy, which is deleted afterwards. The script
checks that the real table counts are unchanged.

Sanity checks (exit 1 if any fails):
  1. agent 1 tracks SPY: with s = shares filled at the day-2 open O (day-1
     close C0), final equity = V0 - s*O*1.0005 + s*P_end - interest - borrow
     to $0.01, if agent 1 filled only once; otherwise its return must sit
     within (slippage + interest + |idle cash x SPY move|) / V0 + 1 bp of SPY's
     open-to-end return
  2. the cash floor is never breached: floored agents' cash >= -$0.000001 at every close
  3. no equity jump on a non-trading day: equity rows only on sessions, one
     per agent per session, so a calendar-day series changes only on sessions
  4. the accounting identity every day for every agent, rebuilt from the
     copy's equity / positions / fills rows and the stored closes:
     dEquity = prior shares x dClose + fill qty x (close - open) - slippage - fees - borrow - interest, to $0.01
  5. gross after each decision's fills is within the agent's §6 limit, as the
     ledger sized it: the filled shares x the decision closes / the portfolio
     value the agent sized with (equity at the decision close) <= max_gross
     + 0.5 pp (room for $100-minimum leftovers). The filled book at the fill
     day's open, gross / equity with cash after the fills, is reported but
     not checked: it moves with the overnight gap, which no limit controls.
  6. complete targets (§7 rule 1, v1.8): after each fill day, until the next
     decision, every stock absent from (or 0 in) the agent's last non-hold
     targets has zero shares, except $100-minimum leftovers (worth under $100
     at that decision's close), which are listed
  7. agent 4 keeps open positions at their drifted weight: no order on a kept
     position unless a §6 clip changed its weight

The SPY reference line is $START_CASH bought at the day-2 open with no costs,
so agent 1 sits just below it by its slippage and idle cash.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data import calendar as cal  # noqa: E402
from data import db  # noqa: E402
from data.snapshot import build_snapshot  # noqa: E402
from execution import ledger as L  # noqa: E402
from execution import ledger_db as LD  # noqa: E402
from execution.risk import apply_risk, profile  # noqa: E402
from scripts.backtest_indicators import no_network  # noqa: E402
from scripts.backtest_ledger import copy_db, counts, store_snapshot_row  # noqa: E402
from strategies import registry  # noqa: E402
from strategies.base import Portfolio, build_context  # noqa: E402

AGENT_IDS = (1, 2, 3, 4, 5, 6)
MIN_HISTORY = 100
END = date(2026, 11, 13)
ANNUAL = 252
TOL_USD = 0.01
GROSS_TOL = 0.005
CASH_FLOOR_TOL = -1e-6
SPY_LABEL = "SPY buy-and-hold (no costs)"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def run_sessions(conn: sqlite3.Connection, end: date, min_history: int = MIN_HISTORY) -> list[date]:
    """Trading sessions from the first with min_history bars for every symbol, to end."""
    dates = [date.fromisoformat(r[0]) for r in conn.execute(
        "SELECT DISTINCT bar_date FROM prices WHERE asset_class = 'us_equity' AND bar_date <= ? ORDER BY bar_date",
        (end.isoformat(),))]
    sessions = [d for d in dates if cal.is_trading_day(d, conn, trading=no_network)]
    per_symbol = defaultdict(list)
    for sym, d in conn.execute("SELECT DISTINCT symbol, bar_date FROM prices WHERE asset_class = 'us_equity'"):
        per_symbol[sym].append(date.fromisoformat(d))
    nth = max(sorted(ds)[min_history - 1] for ds in per_symbol.values())   # every symbol has min_history bars
    return [d for d in sessions if d >= nth]


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def run(db_path: Path, end: date = END, max_sessions: int | None = None, progress: bool = True) -> dict:
    before = counts(db_path)
    now = datetime.now(timezone.utc)
    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        conn = copy_db(db_path, Path(tmp) / "backtest.db")
        try:
            sessions = run_sessions(conn, end)
            if max_sessions:
                sessions = sessions[:max_sessions]
            agents = {a: {"name": registry.NAMES[a], "days": []} for a in AGENT_IDS}
            a4 = {"decisions": 0, "kept": 0, "unclipped_resizes": [], "clipped_resizes": 0}
            for i, d in enumerate(sessions):
                snap = build_snapshot(conn, "us_equity", d, "backtest", trading=no_network)
                sid = store_snapshot_row(conn, snap)
                closes = {s: f["last_close"] for s, f in snap.payload["symbols"].items()}
                ctx = build_context(d, sessions[:i + 1])
                decided_at = cal.equity_times(d, conn, no_network).decide_at
                for agent_id in AGENT_IDS:
                    aid = str(agent_id)
                    res = LD.settle_day(conn, aid, d, now, mode="backtest", trading=no_network)
                    acct = LD.load_account(conn, aid, as_of=d)
                    pv = acct.value(closes)
                    portfolio = Portfolio(cash=acct.cash, positions={s: p.qty for s, p in acct.positions.items()},
                                          value=pv)
                    dec = registry.get_agent(agent_id)(snap, portfolio, ctx)
                    current = {s: p.qty * closes[s] / pv for s, p in acct.positions.items()}
                    weights, events = apply_risk(agent_id, dec, {s: True for s in closes}, current)
                    with conn:
                        did = conn.execute(
                            "INSERT INTO decisions (agent_id, snapshot_id, decided_at, status, parsed_targets, "
                            "overall_reasoning) VALUES (?, ?, ?, 'ok', ?, ?)",
                            (aid, sid, db.utc_ts(decided_at), json.dumps(weights), dec.reason)).lastrowid
                        LD.write_risk_events(conn, aid, events, decided_at, did)
                    placed = LD.place_orders(conn, aid, d, closes, weights, decided_at, did)
                    if agent_id == 4 and weights is not None:
                        kept = {s for s, w in weights.items() if acct.shares(s) * w > 0}
                        clipped = {e["symbol"] for e in events}
                        a4["decisions"] += 1
                        a4["kept"] += len(kept)
                        for o in placed.orders:
                            if o.symbol in kept:
                                if o.symbol in clipped:
                                    a4["clipped_resizes"] += 1
                                else:
                                    a4["unclipped_resizes"].append((d, o.symbol, o.side, o.qty))
                    agents[agent_id]["days"].append({
                        "date": d, "equity": res.mark.equity, "cash": res.mark.cash, "fills": res.fills,
                        "borrow": res.borrow, "interest": res.interest, "clips": len(events),
                        "margin": len(placed.margin_events), "floor": res.floor_events})
                if progress and (i + 1) % 50 == 0:
                    print(f"  {i + 1}/{len(sessions)} sessions ({time.time() - t0:.0f}s)", file=sys.stderr)
            spy = spy_series(conn, sessions, now)
            report = {"sessions": sessions, "agents": agents, "spy": spy,
                      "checks": {
                          "agent1_tracks_spy": check_agent1(conn, sessions, agents[1]["days"], now),
                          "cash_floor": check_cash_floor(agents),
                          "no_off_session_equity": check_sessions(conn, sessions),
                          "accounting_identity": check_identity(conn, sessions, now),
                          "gross_after_fills": check_gross(conn, sessions, now),
                          "complete_targets": check_complete_targets(conn, sessions, now),
                          "agent4_no_resize": {**a4, "ok": a4["decisions"] > 0 and not a4["unclipped_resizes"]},
                      }}
        finally:
            conn.close()
    report["real_db_unchanged"] = counts(db_path) == before
    report["seconds"] = time.time() - t0
    return report


def spy_series(conn, sessions, now) -> list[float]:
    """$START_CASH in SPY bought at the day-2 open (when agent 1 fills), no costs; cash on day 1."""
    o = LD.day_bars(conn, ["SPY"], sessions[1], now)["SPY"]["adj_open"]
    c = [LD.day_bars(conn, ["SPY"], d, now)["SPY"]["adj_close"] for d in sessions[1:]]
    return [L.START_CASH] + [L.START_CASH * x / o for x in c]


# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------

def check_agent1(conn, sessions, days, now) -> dict:
    bars = {d: LD.day_bars(conn, ["SPY"], d, now)["SPY"] for d in sessions[:2] + sessions[-1:]}
    c0, o, p_end = bars[sessions[0]]["adj_close"], bars[sessions[1]]["adj_open"], bars[sessions[-1]]["adj_close"]
    v0, v_end = days[0]["equity"], days[-1]["equity"]
    fills = [f for x in days for f in x["fills"]]
    interest, borrow = sum(x["interest"] for x in days), sum(x["borrow"] for x in days)
    slippage = sum(f.slippage_usd for f in fills)
    s = sum(f.signed_qty for f in fills if f.symbol == "SPY")
    out = {"c0": c0, "open": o, "p_end": p_end, "n_fills": len(fills), "shares": s,
           "spy_open_to_end": p_end / o - 1, "spy_close_to_end": p_end / c0 - 1,
           "agent_return": v_end / v0 - 1, "slippage_usd": slippage, "interest": interest,
           "final_cash": days[-1]["cash"], "floor_scaled": any(x["floor"] for x in days)}
    if len(fills) == 1:
        predicted = v0 - s * o * (1 + L.SLIPPAGE_BPS / 1e4) + s * p_end - interest - borrow
        out.update(mode="exact formula, one fill", predicted_end=predicted, diff_usd=v_end - predicted,
                   ok=abs(v_end - predicted) < TOL_USD)
    else:
        idle = max(abs(x["cash"]) for x in days)
        bound = (slippage + interest + borrow + idle * abs(p_end / o - 1)) / v0 + 1e-4
        gap = out["agent_return"] - out["spy_open_to_end"]
        out.update(mode="tracking bound, several fills", gap=gap, bound=bound, ok=abs(gap) <= bound)
    return out


def check_cash_floor(agents) -> dict:
    out = {}
    for a, rec in agents.items():
        if L.has_cash_floor(a):
            worst = min(rec["days"], key=lambda x: x["cash"])
            out[a] = {"min_cash": worst["cash"], "on": worst["date"], "ok": worst["cash"] >= CASH_FLOOR_TOL}
    return {"agents": out, "ok": all(v["ok"] for v in out.values())}


def check_sessions(conn, sessions) -> dict:
    rows = conn.execute("SELECT agent_id, as_of_date, COUNT(*) FROM equity GROUP BY agent_id, as_of_date").fetchall()
    off = sorted({r[1] for r in rows if not cal.is_trading_day(date.fromisoformat(r[1]), conn, trading=no_network)})
    dup = [(r[0], r[1]) for r in rows if r[2] != 1]
    want = {(str(a), d.isoformat()) for a in AGENT_IDS for d in sessions}
    have = {(r[0], r[1]) for r in rows}
    # calendar-day forward fill: any change must land on a session
    jumps = 0
    for a in AGENT_IDS:
        eq = dict(conn.execute("SELECT as_of_date, equity FROM equity WHERE agent_id = ?", (str(a),)).fetchall())
        prev, d = None, sessions[0]
        while d <= sessions[-1]:
            v = eq.get(d.isoformat(), prev)
            if prev is not None and v != prev and d not in set(sessions):
                jumps += 1
            prev, d = v, d + timedelta(days=1)
    return {"off_session_rows": off, "duplicates": dup, "missing": sorted(want - have)[:5],
            "calendar_day_jumps_off_session": jumps,
            "ok": not off and not dup and want == have and jumps == 0}


def check_identity(conn, sessions, now) -> dict:
    worst, n, bad = 0.0, 0, []
    for a in AGENT_IDS:
        aid = str(a)
        eq = {r["as_of_date"]: r for r in conn.execute("SELECT * FROM equity WHERE agent_id = ?", (aid,))}
        pos = defaultdict(dict)
        for r in conn.execute("SELECT as_of_date, symbol, qty FROM positions WHERE agent_id = ?", (aid,)):
            pos[r["as_of_date"]][r["symbol"]] = r["qty"]
        fills = defaultdict(list)
        for r in conn.execute(
                "SELECT f.filled_at, o.symbol, o.side, f.qty, f.reference_price, f.fill_price, f.fee_usd "
                "FROM fills f JOIN orders o ON o.order_id = f.order_id "
                "WHERE o.agent_id = ? AND f.status = 'filled'", (aid,)):
            fills[r["filled_at"][:10]].append(r)
        first = eq[sessions[0].isoformat()]
        if abs(first["equity"] - L.START_CASH) > TOL_USD:
            bad.append((a, sessions[0], first["equity"] - L.START_CASH))
        for prev_d, d in zip(sessions, sessions[1:]):
            p, k = prev_d.isoformat(), d.isoformat()
            syms = sorted(set(pos[p]) | {f["symbol"] for f in fills[k]})
            c_now = {s: r["adj_close"] for s, r in LD.day_bars(conn, syms, d, now).items()}
            c_prev = {s: r["adj_close"] for s, r in LD.day_bars(conn, list(pos[p]), prev_d, now).items()}
            pnl = math.fsum(q * (c_now[s] - c_prev[s]) for s, q in pos[p].items())
            slip = fee = 0.0
            for f in fills[k]:
                sq = f["qty"] if f["side"] == "buy" else -f["qty"]
                pnl += sq * (c_now[f["symbol"]] - f["reference_price"])
                slip += f["qty"] * abs(f["fill_price"] - f["reference_price"])
                fee += f["fee_usd"]
            e = eq[k]
            expected = pnl - slip - fee - e["borrow_fee_usd"] - e["margin_interest_usd"]
            diff = (e["equity"] - eq[p]["equity"]) - expected
            worst, n = max(worst, abs(diff)), n + 1
            if abs(diff) >= TOL_USD:
                bad.append((a, d, diff))
    return {"days_checked": n, "worst_abs_diff_usd": worst, "failures": bad[:10], "ok": not bad}


def _books(conn, aid):
    """{date: {symbol: qty}}, {date: equity row}, [(decision date, risk-checked targets)] for one agent."""
    pos = defaultdict(dict)
    for r in conn.execute("SELECT as_of_date, symbol, qty FROM positions WHERE agent_id = ?", (aid,)):
        if r["qty"] != 0:
            pos[r["as_of_date"]][r["symbol"]] = r["qty"]
    eq = {r["as_of_date"]: r for r in conn.execute("SELECT * FROM equity WHERE agent_id = ?", (aid,))}
    decs = [(date.fromisoformat(r["snapshot_date"]), json.loads(r["parsed_targets"])) for r in conn.execute(
        "SELECT s.snapshot_date, d.parsed_targets FROM decisions d JOIN snapshots s USING (snapshot_id) "
        "WHERE d.agent_id = ? ORDER BY s.snapshot_date", (aid,))]
    return pos, eq, [(d, t) for d, t in decs if t is not None]


def check_gross(conn, sessions, now) -> dict:
    nxt = dict(zip(sessions, sessions[1:]))
    out, bad = {}, []
    for a in AGENT_IDS:
        aid, limit = str(a), profile(a).max_gross
        pos, eq, decs = _books(conn, aid)
        worst = worst_open = 0.0
        worst_on = None
        for d, _ in decs:
            f = nxt.get(d)
            if f is None:
                continue                                    # the last decision is never filled
            book = pos[f.isoformat()]
            e = eq[f.isoformat()]
            cash = e["cash"] + e["borrow_fee_usd"] + e["margin_interest_usd"]   # before the close's accruals
            c_d = {s: r["adj_close"] for s, r in LD.day_bars(conn, list(book), d, now).items()}
            o_f = {s: r["adj_open"] for s, r in LD.day_bars(conn, list(book), f, now).items()}
            ratio = math.fsum(abs(q * c_d[s]) for s, q in book.items()) / eq[d.isoformat()]["equity"]
            if ratio > worst:
                worst, worst_on = ratio, f
            if ratio > limit + GROSS_TOL:
                bad.append((a, f, round(ratio, 4)))
            at_open = (math.fsum(abs(q * o_f[s]) for s, q in book.items())
                       / (cash + math.fsum(q * o_f[s] for s, q in book.items())))
            worst_open = max(worst_open, at_open)
        out[a] = {"limit": limit, "worst": worst, "on": worst_on, "worst_at_open": worst_open}
    return {"agents": out, "failures": bad[:10], "n_failures": len(bad), "ok": not bad}


def check_complete_targets(conn, sessions, now) -> dict:
    nxt = dict(zip(sessions, sessions[1:]))
    leftovers, bad = [], []
    for a in AGENT_IDS:
        aid = str(a)
        pos, _, decs = _books(conn, aid)
        bounds = [nxt.get(d) for d, _ in decs[1:]] + [None]          # until the next decision's fill day
        for (d, targets), until in zip(decs, bounds):
            start = nxt.get(d)
            if start is None:
                continue
            closes_d = None
            for day in sessions[sessions.index(start):]:
                if until is not None and day >= until:
                    break
                for s, q in pos[day.isoformat()].items():
                    if targets.get(s, 0.0) != 0:
                        continue
                    if closes_d is None:
                        closes_d = {k: r["adj_close"] for k, r in LD.day_bars(
                            conn, sorted({k for v in pos.values() for k in v}), d, now).items()}
                    value = q * closes_d[s] if s in closes_d else float("inf")
                    if abs(value) < L.MIN_TRADE_USD:
                        if (a, d, s) not in {(x[0], x[1], x[2]) for x in leftovers}:
                            leftovers.append((a, d, s, q, value))
                    else:
                        bad.append((a, d, day, s, q, value))
    return {"leftovers": leftovers, "failures": bad[:10], "n_failures": len(bad), "ok": not bad}


# ---------------------------------------------------------------------------
# Metrics and outputs
# ---------------------------------------------------------------------------

def metrics(equity: list[float]) -> dict:
    rets = [b / a - 1 for a, b in zip(equity, equity[1:])]
    sd = statistics.stdev(rets) if len(rets) > 1 else 0.0
    peak, mdd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {"total_return": equity[-1] / equity[0] - 1, "ann_vol": sd * math.sqrt(ANNUAL),
            "sharpe": statistics.fmean(rets) / sd * math.sqrt(ANNUAL) if sd else float("nan"),
            "max_drawdown": mdd}


def summary(r: dict) -> list[dict]:
    rows = []
    for a, rec in r["agents"].items():
        days = rec["days"]
        fills = [f for d in days for f in d["fills"]]
        floor = [e for d in days for e in d["floor"]]
        rows.append({
            "agent": a, "name": rec["name"], **metrics([d["equity"] for d in days]),
            "final_equity": days[-1]["equity"], "trades": len(fills),
            "slippage_usd": sum(f.slippage_usd for f in fills), "fees_usd": sum(f.fee_usd for f in fills),
            "borrow_usd": sum(d["borrow"] for d in days), "interest_usd": sum(d["interest"] for d in days),
            "risk_clips": sum(d["clips"] for d in days), "cash_floor_events": len(floor),
            "cash_floor_cancels": sum("cancelled" in json.loads(e["detail"]) for e in floor),
            "margin_events": sum(d["margin"] for d in days)})
    m = metrics(r["spy"])
    rows.append({"agent": "SPY", "name": SPY_LABEL, **m, "final_equity": r["spy"][-1]})
    return rows


def write_outputs(r: dict, rows: list[dict], out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    cols = [f"{a}_{rec['name']}" for a, rec in r["agents"].items()]
    eq_path = out / "equity_daily.csv"
    with eq_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", *cols, "spy_buy_and_hold_no_costs"])
        for i, d in enumerate(r["sessions"]):
            w.writerow([d.isoformat(), *(f"{rec['days'][i]['equity']:.6f}" for rec in r["agents"].values()),
                        f"{r['spy'][i]:.6f}"])
    sum_path = out / "summary.csv"
    keys = list(rows[0])
    with sum_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        for row in rows:
            w.writerow({k: (f"{v:.6f}" if isinstance(v, float) else v) for k, v in row.items()})
    png_path = out / "equity_curves.png"
    plot(r, png_path)
    return [eq_path, sum_path, png_path]


# Reference categorical palette (dataviz skill), fixed order; validated on the light surface.
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300")
SURFACE, INK, INK_2, GRID, SPY_GRAY = "#fcfcfb", "#1f1e1c", "#52514e", "#e6e5e0", "#8a8984"


def plot(r: dict, path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    x = r["sessions"]
    fig, ax = plt.subplots(figsize=(11, 6.2), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ends = []
    ax.plot(x, r["spy"], color=SPY_GRAY, lw=1.6, ls=(0, (4, 3)), label=SPY_LABEL + ", from the day-2 open",
            zorder=2)
    ends.append((r["spy"][-1], "SPY (no costs)", SPY_GRAY))
    for (a, rec), color in zip(r["agents"].items(), SERIES):
        y = [d["equity"] for d in rec["days"]]
        ax.plot(x, y, color=color, lw=2, label=f"{a} · {rec['name']}", zorder=3)
        ends.append((y[-1], f"{a} · {rec['name']}", color))
    # direct labels at the right end, nudged apart
    lo, hi = ax.get_ylim()
    gap = (hi - lo) * 0.035
    placed = []
    for v, text, color in sorted(ends):
        y = max(v, placed[-1] + gap) if placed else v
        placed.append(y)
        ax.annotate(text, xy=(x[-1], v), xytext=(x[-1] + timedelta(days=6), y), color=INK_2, fontsize=8.5,
                    va="center", annotation_clip=False,
                    arrowprops={"arrowstyle": "-", "color": color, "lw": 1})
    ax.axhline(L.START_CASH, color=INK_2, lw=0.8, zorder=1)
    ax.grid(axis="y", color=GRID, lw=0.8)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=9, length=0)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v / 1000:,.0f}k"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.set_xlim(x[0], x[-1])
    ax.legend(loc="upper left", frameon=False, fontsize=8.5, labelcolor=INK_2, ncol=2)
    fig.suptitle("Six control agents through the ledger, equity per agent", x=0.06, ha="left",
                 color=INK, fontsize=13, fontweight="bold")
    ax.set_title(f"$100k each, {x[0]:%d %b %Y} – {x[-1]:%d %b %Y}, {len(x)} sessions. "
                 "Backtest with today's data and split adjustment: not a forecast.",
                 loc="left", color=INK_2, fontsize=9)
    fig.subplots_adjust(left=0.06, right=0.84, top=0.88, bottom=0.08)
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def format_table(rows: list[dict]) -> str:
    head = (f"{'agent':<22}{'return':>9}{'vol':>8}{'Sharpe':>8}{'maxDD':>9}{'trades':>8}"
            f"{'slip $':>10}{'fees $':>8}{'borrow $':>10}{'int $':>8}{'clips':>7}{'floor':>7}")
    lines = [head]
    for x in rows:
        lines.append(
            f"{(str(x['agent']) + ' ' + x['name'])[:21]:<22}{x['total_return']:>+9.2%}{x['ann_vol']:>8.2%}"
            f"{x['sharpe']:>8.2f}{x['max_drawdown']:>+9.2%}"
            + (f"{x['trades']:>8}{x['slippage_usd']:>10,.2f}{x['fees_usd']:>8,.2f}{x['borrow_usd']:>10,.2f}"
               f"{x['interest_usd']:>8,.2f}{x['risk_clips']:>7}{x['cash_floor_events']:>7}" if "trades" in x else ""))
    return "\n".join(lines)


def format_checks(r: dict) -> str:
    c = r["checks"]
    a1, cf, ss, ai = c["agent1_tracks_spy"], c["cash_floor"], c["no_off_session_equity"], c["accounting_identity"]
    ok = lambda b: "OK  " if b else "FAIL"  # noqa: E731
    a1_line = (f"final equity diff vs formula ${a1['diff_usd']:+.6f}" if "diff_usd" in a1
               else f"gap {a1['gap']:+.4%} vs bound {a1['bound']:.4%}")
    return "\n".join([
        f"[{ok(a1['ok'])}] 1. agent 1 tracks SPY ({a1['mode']}): agent {a1['agent_return']:+.4%}, "
        f"SPY open->end {a1['spy_open_to_end']:+.4%}, close->end {a1['spy_close_to_end']:+.4%}; "
        f"{a1_line}; slippage ${a1['slippage_usd']:,.2f}, interest ${a1['interest']:,.2f}, "
        f"final cash ${a1['final_cash']:,.2f}{', buy scaled by the cash floor' if a1['floor_scaled'] else ''}",
        f"[{ok(cf['ok'])}] 2. cash floor: min cash "
        + ", ".join(f"agent {a} ${v['min_cash']:,.6f} ({v['on']})" for a, v in cf["agents"].items()),
        f"[{ok(ss['ok'])}] 3. equity only on sessions: {len(ss['off_session_rows'])} off-session rows, "
        f"{len(ss['duplicates'])} duplicates, {len(ss['missing'])} missing, "
        f"{ss['calendar_day_jumps_off_session']} calendar-day jumps off session",
        f"[{ok(ai['ok'])}] 4. accounting identity: {ai['days_checked']} agent-days, worst |diff| "
        f"${ai['worst_abs_diff_usd']:.8f}" + (f", failures {ai['failures']}" if ai["failures"] else ""),
        f"[{ok(c['gross_after_fills']['ok'])}] 5. gross after decision fills (filled shares x decision closes / sizing value) "
        f"within §6 limit + {GROSS_TOL:.1%}: "
        + "; ".join(f"agent {a} worst {v['worst']:.4f} / {v['limit']:.2f} (book at the fill-day open, not checked: {v['worst_at_open']:.4f})"
                    for a, v in c["gross_after_fills"]["agents"].items())
        + (f"; failures {c['gross_after_fills']['n_failures']}: {c['gross_after_fills']['failures']}"
           if c["gross_after_fills"]["failures"] else ""),
        f"[{ok(c['complete_targets']['ok'])}] 6. stocks absent from the last targets hold zero shares: "
        f"{c['complete_targets']['n_failures']} violations, {len(c['complete_targets']['leftovers'])} "
        f"$100-minimum leftovers"
        + "".join(f"\n         leftover: agent {a}, decided {d}, {s} {q:.9f} shares (${v:,.2f})"
                  for a, d, s, q, v in c["complete_targets"]["leftovers"])
        + (f"\n         failures: {c['complete_targets']['failures']}" if c["complete_targets"]["failures"] else ""),
        f"[{ok(c['agent4_no_resize']['ok'])}] 7. agent 4 no resizing: {c['agent4_no_resize']['decisions']} decisions, "
        f"{c['agent4_no_resize']['kept']} kept positions listed, "
        f"{len(c['agent4_no_resize']['unclipped_resizes'])} orders on kept positions without a §6 clip, "
        f"{c['agent4_no_resize']['clipped_resizes']} with a clip"
        + (f" {c['agent4_no_resize']['unclipped_resizes'][:5]}" if c["agent4_no_resize"]["unclipped_resizes"] else ""),
        f"[{ok(r['real_db_unchanged'])}]    real database unchanged",
    ])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    p.add_argument("--end", type=date.fromisoformat, default=END)
    p.add_argument("--sessions", type=int, default=None, help="only the first N sessions (a quick look)")
    p.add_argument("--out", type=Path, default=ROOT / "analysis" / "backtests" / date.today().isoformat())
    p.add_argument("--no-out", action="store_true")
    args = p.parse_args(argv)
    r = run(args.db, args.end, args.sessions)
    rows = summary(r)
    s = r["sessions"]
    print(f"{len(s)} sessions {s[0]} .. {s[-1]} (backtest snapshots, ledger in backtest mode, temp DB copy, "
          f"{r['seconds']:.0f}s)")
    print("BACKTEST WITH TODAY'S DATA, NOT A FORECAST.\n")
    print(format_table(rows))
    print()
    print(format_checks(r))
    if not args.no_out:
        for f in write_outputs(r, rows, args.out):
            print("wrote", f.relative_to(ROOT) if f.is_relative_to(ROOT) else f)
    ok = all(c["ok"] for c in r["checks"].values()) and r["real_db_unchanged"]
    print("\nCHECKS", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
