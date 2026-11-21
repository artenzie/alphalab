"""The daily runner: one command that runs a full trading day in protocol order (§3, §13).

    python -m runner.daily equity --experiment shakedown            # the latest closed session
    python -m runner.daily crypto --experiment shakedown --date 2026-12-14
    python -m runner.daily equity --db /tmp/dev.db --date 2026-11-18

Equity day D (§3; systemd starts it at close + 1:30):
  1. ingest D's daily bars + data checks (data/ingest.py)
  2. settle_day(D) for every agent: fill D-1's orders at D's open, accrue, mark at D's close
  3. at close + 1:45: build and store the live snapshot (hashed; data checks as of its cutoff)
  4. gate: a snapshot that failed its checks (or a run past the deadline) -> no agent decides
  5. easy-to-borrow flags from Alpaca for the short-selling agents
  6. at close + 2:00: every agent decides. Rule agents in turn, AI agents in parallel threads
     (so a provider outage costs ~30 minutes in all, not per agent)
  7. per agent: decision rows (one per AI attempt) + apply_risk events, in one transaction
  8. place_orders: margin check, then pending orders (filled by settle_day(D+1))
Crypto day D (§13; starts at 00:05Z on D+1): step 0 fetches the 1-minute fill bars after
D-1's decision (data/fill_prices.py), which settle_day(D) fills at; then the same steps at
00:05Z / 00:10Z / 00:15Z, with no easy-to-borrow step (long only).

Crypto decision time t_ref (§7 rule 4, v1.13): one per day, shared by every crypto agent,
the BTC buy-and-hold control included: the time the run's last crypto decision was
recorded (decisions.decided_at), never the scheduled 00:15Z. So the crypto orders are
placed only after every crypto agent has decided, all with created_at = t_ref, and the
next day's step 0 fetches the window [t_ref, t_ref + 60 min) (rounded up to the minute).
Once a day's crypto orders are placed, its t_ref is fixed: an agent that decides after it (in
a rerun) is missed for the day (no order, labelled late), never given a second t_ref.

Late runs (protocol v1.13). If the data step finishes after the protocol snapshot time,
the run is late: it doesn't wait, the snapshot's cutoff is its actual build time and it
is labelled late (minutes late in the payload and heartbeat). Agents may decide until the
deadline: 1 hour before the next session's open (equity) or the next 00:15Z decision
(crypto). After it nothing decides or is placed; the ledger still settles and marks.
If the snapshot fails its checks before the deadline, no agent decides but each settled
agent still gets the margin check (place_orders with a hold, §7 rule 8).

Restart safety: running a date again never creates a second snapshot, decision, order
or fill. Ingest and fill bars are skipped once every agent is settled and the snapshot
is stored (and are versioned no-ops anyway); settle_day and place_orders have their own
guards; a stored snapshot is rebuilt and must hash the same; an agent with decision
rows for the snapshot is never called again (its stored targets and risk events give
the same orders); and an agent with orders or a margin event already placed for the day
is skipped.

Heartbeat (§9): one run_id per run. Job 'daily_run' (started / finished / error), one
started + finished/error per step ('fetch_prices', 'data_checks', 'fetch_fill_prices',
'build_snapshot' are written by their modules with the same run_id), and per-agent rows
'settle:<id>', 'decide:<id>', 'place_orders:<id>'.

Database: one file per experiment (runner/experiment.py); the run refuses a file
without its label or a date before the experiment's start. A lock file per database
and asset class keeps two runs of the same kind from overlapping.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sqlite3
import sys
import time
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from agents.harness import BACKOFF_S, BLEND_OF, BLENDER_ID, ELIGIBLE_AGENTS, blend
from data import calendar as cal
from data import db
from data import snapshot as sn
from execution import ledger as L
from execution import ledger_db as LD
from execution.risk import apply_eligibility, apply_risk, profile
from runner import blender as BL
from runner import experiment as ex
from runner import roster
from strategies.base import AgentMemory, Decision, PastDecision, Portfolio, build_context, ret63_top

JOB = "daily_run"
NO_AI_LABEL = "dev"                 # --no-ai runs only on a dev experiment, never shakedown or official
ASSET_CLASSES = {"equity": "us_equity", "crypto": "crypto"}
LAST_DECISIONS = 5
WAIT_STEP_S = 60.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Dependencies (real by default, fakes in tests)
# ---------------------------------------------------------------------------

@dataclass
class Deps:
    """Everything with side effects outside the database."""
    now: Callable[[], datetime] = _utcnow
    sleep: Callable[[float], None] = time.sleep
    # (conn, asset_class, run_id, now) -> exit code
    ingest: Callable[..., int] | None = None
    # (conn, decision_dates, run_id, now, window_starts) -> exit code
    fill_prices: Callable[..., int] | None = None
    # symbols -> {symbol: easy_to_borrow}
    etb: Callable[[list[str]], dict[str, bool]] | None = None
    # agent_id -> a fresh LLMClient (one per AI agent: threads share nothing)
    client_for: Callable[[str], Any] | None = None
    trading: Any = None                      # Alpaca trading client for the calendar (None = default)
    ai_sleep: Callable[[float], None] = time.sleep
    ai_backoff: tuple[float, ...] = BACKOFF_S      # §8: 3 retries over 30 minutes


def real_deps() -> Deps:
    """Alpaca + OpenRouter, keys from .env (never printed)."""
    from dotenv import load_dotenv

    from agents.openrouter import OpenRouterClient
    from data import fill_prices as fp
    from data import ingest

    load_dotenv(db.DATA_DIR.parent / ".env")
    clients = ingest.make_clients()

    def do_ingest(conn, asset_class, run_id, now):
        eq = ingest.load_universe() if asset_class == "us_equity" else []
        cr = ingest.CRYPTO_SYMBOLS if asset_class == "crypto" else ()
        return ingest.run(conn, clients, days=5, now=now, equity_symbols=eq, crypto_symbols=cr, run_id=run_id)

    def do_fill_prices(conn, dates, run_id, now, window_starts=None):
        return fp.run(conn, clients, dates, now=now, run_id=run_id, window_starts=window_starts)

    def etb(symbols):
        out = {}
        for s in symbols:
            a = ingest.with_retries(lambda: clients.trading.get_asset(s), f"get_asset {s}", time.sleep)
            out[s] = bool(getattr(a, "easy_to_borrow", False))
        return out

    return Deps(ingest=do_ingest, fill_prices=do_fill_prices, etb=etb,
                client_for=lambda aid: OpenRouterClient(), trading=clients.trading)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, allow_nan=False)


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:1000]


class Heartbeat:
    def __init__(self, conn: sqlite3.Connection, run_id: str, now: Callable[[], datetime]):
        self.conn, self.run_id, self.now = conn, run_id, now
        self.errors: list[str] = []

    def __call__(self, job: str, event: str, **detail: Any) -> None:
        if event == "error":
            self.errors.append(f"{job}: {detail.get('error', detail.get('reason', ''))}")
        with self.conn:
            self.conn.execute("INSERT INTO heartbeat (run_id, job, event, at, detail) VALUES (?, ?, ?, ?, ?)",
                              (self.run_id, job, event, db.utc_ts(self.now()), _json(detail)))


def _insert(conn: sqlite3.Connection, table: str, row: Mapping[str, Any]) -> int:
    cols = ", ".join(row)
    return conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(row))})",
                        list(row.values())).lastrowid


def wait_until(t: datetime, deps: Deps) -> None:
    while (left := (t - deps.now()).total_seconds()) > 0:
        deps.sleep(min(left, WAIT_STEP_S))


def default_date(asset_class: str, now: datetime, conn: sqlite3.Connection, trading: Any = None) -> date:
    """Equity: the latest session whose bars fetch time (close + 1:30) has passed.
    Crypto: the latest day whose 00:05Z fetch on the next day has passed."""
    if asset_class == "crypto":
        d = now.date() - timedelta(days=1)
        while cal.crypto_times(d).bars_fetch_at > now:
            d -= timedelta(days=1)
        return d
    d = cal.to_et(now).date()
    for _ in range(15):
        if cal.session(d, conn, trading) is not None and cal.equity_times(d, conn, trading).bars_fetch_at <= now:
            return d
        d -= timedelta(days=1)
    raise cal.CalendarError(f"no closed session in the 15 days before {now}")


def run_sessions(conn: sqlite3.Connection, asset_class: str, start: date, day: date, trading: Any) -> list[date]:
    days = [start + timedelta(days=i) for i in range((day - start).days + 1)]
    if asset_class == "crypto":
        return days
    return [d for d in days if cal.session(d, conn, trading) is not None]


def trading_days_left(conn: sqlite3.Connection, asset_class: str, day: date, end: date | None, trading: Any) -> int:
    """The run's trading days after `day` up to `end` (v1.15): NYSE sessions, or calendar days for crypto."""
    if end is None:
        raise ex.ExperimentError("the experiment label has no end date (made before v1.15); "
                                 "AI agents need it for input.run")
    if day > end:
        raise ex.ExperimentError(f"{day} is after the experiment's end date {end}")
    if day == end:
        return 0
    return len(run_sessions(conn, asset_class, day + timedelta(days=1), end, trading))


# ---------------------------------------------------------------------------
# What is already stored (restart safety)
# ---------------------------------------------------------------------------

def stored_snapshot(conn: sqlite3.Connection, asset_class: str, day: date) -> sqlite3.Row | None:
    return conn.execute("""SELECT * FROM snapshots WHERE asset_class = ? AND snapshot_date = ?
                           AND corrects_id IS NULL""", (asset_class, day.isoformat())).fetchone()


def is_settled(conn: sqlite3.Connection, agent_id: str, day: date) -> bool:
    return conn.execute("SELECT 1 FROM equity WHERE agent_id = ? AND as_of_date = ?",
                        (agent_id, day.isoformat())).fetchone() is not None


def final_decision(conn: sqlite3.Connection, agent_id: str, snapshot_id: int) -> sqlite3.Row | None:
    return conn.execute("""SELECT * FROM decisions WHERE agent_id = ? AND snapshot_id = ? AND corrects_id IS NULL
                           ORDER BY attempt DESC, decision_id DESC LIMIT 1""", (agent_id, snapshot_id)).fetchone()


def already_placed(conn: sqlite3.Connection, agent_id: str, start: datetime, end: datetime) -> bool:
    """Orders (decision / margin) or a margin event for the agent in [start, end): the day's placement."""
    p = {"a": agent_id, "s": db.utc_ts(start), "e": db.utc_ts(end)}
    return conn.execute(
        """SELECT EXISTS (SELECT 1 FROM orders WHERE agent_id = :a AND venue = 'ledger'
                            AND origin IN ('decision', 'margin') AND created_at >= :s AND created_at < :e)
               OR EXISTS (SELECT 1 FROM risk_events WHERE agent_id = :a AND rule = 'margin'
                            AND occurred_at >= :s AND occurred_at < :e)""", p).fetchone()[0] == 1


def crypto_window_starts(conn: sqlite3.Connection, agent_ids, decision_date: date) -> list[datetime]:
    """The fill-window start(s) for decision_date's crypto orders still unfilled: ceil(t_ref) per
    distinct created_at (one, unless a rerun placed agents at different times). [] = no orders."""
    lo = cal.crypto_times(decision_date).decide_at
    hi = cal.crypto_times(decision_date + timedelta(days=1)).snapshot_at
    names = ", ".join("?" * len(agent_ids))
    rows = conn.execute(
        f"""SELECT DISTINCT o.created_at FROM orders AS o
            WHERE o.agent_id IN ({names}) AND o.venue = 'ledger' AND o.created_at >= ? AND o.created_at <= ?
              AND NOT EXISTS (SELECT 1 FROM fills AS f WHERE f.order_id = o.order_id)""",
        [*agent_ids, db.utc_ts(lo), db.utc_ts(hi)]).fetchall()
    return sorted({LD.ceil_minute(max(lo, cal._parse_ts(r[0]))) for r in rows})


def applied_weights(conn: sqlite3.Connection, decision: sqlite3.Row) -> dict[str, float] | None:
    """A stored decision's weights after risk: its targets with each clipped symbol's last applied weight."""
    if decision["status"] != "ok":
        return None
    targets = json.loads(decision["parsed_targets"])
    if targets is None:
        return None
    for e in conn.execute("""SELECT symbol, applied_weight FROM risk_events WHERE decision_id = ?
                             AND symbol IS NOT NULL ORDER BY risk_event_id""", (decision["decision_id"],)):
        targets[e["symbol"]] = e["applied_weight"]
    return targets


def past_decisions(conn: sqlite3.Connection, agent_id: str, asset_class: str, before: date) -> tuple[PastDecision, ...]:
    """The agent's own last decisions (final attempt per day) before `before`, oldest first (§4).

    Agent 13 (v1.16): only its AI calls count (rows with a model id), not the days it just followed
    agents 3-6 or held without a call, and each shows the allocation it set."""
    ai_only = "AND q.model_id IS NOT NULL" if agent_id == BLENDER_ID else ""
    rows = conn.execute(
        f"""SELECT d.*, s.snapshot_date FROM decisions AS d JOIN snapshots AS s ON s.snapshot_id = d.snapshot_id
           WHERE d.agent_id = :a AND s.asset_class = :ac AND s.snapshot_date < :d AND d.corrects_id IS NULL
             AND d.attempt = (SELECT MAX(q.attempt) FROM decisions AS q WHERE q.agent_id = d.agent_id
                              AND q.snapshot_id = d.snapshot_id AND q.corrects_id IS NULL {ai_only})
           ORDER BY s.snapshot_date DESC LIMIT :n""",
        {"a": agent_id, "ac": asset_class, "d": before.isoformat(), "n": LAST_DECISIONS}).fetchall()
    out = []
    for r in reversed(rows):
        targets = json.loads(r["parsed_targets"]) if r["status"] == "ok" and r["parsed_targets"] else None
        alloc = json.loads(r["parsed_allocation"]) if r["status"] == "ok" and r["parsed_allocation"] else None
        out.append(PastDecision(date.fromisoformat(r["snapshot_date"]), r["status"], targets,
                                json.loads(r["reasons"]) if r["reasons"] else {},
                                r["overall_reasoning"] or r["error"] or "", alloc))
    return tuple(out)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

@dataclass
class AgentReport:
    agent_id: str
    kind: str
    settle: str = ""
    decision: str = ""                    # status, 'stored', 'not decided', ...
    asked: Mapping[str, float] | None = None
    applied: Mapping[str, float] | None = None
    risk_events: int = 0
    orders: tuple = ()
    placed: str = ""
    reason: str = ""
    cost_usd: float | None = None
    attempts: int = 0
    equity: float | None = None


@dataclass
class RunReport:
    run_id: str
    asset_class: str
    day: date
    status: str = "ok"                    # 'ok' | 'error' | 'market_closed'
    late: bool = False
    minutes_late: int = 0
    snapshot_id: int | None = None
    checks_passed: bool | None = None
    decided: bool = False
    skipped: tuple[str, ...] = ()
    agents: dict[str, AgentReport] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if self.errors else 0


class _Day:
    """State of one run of one asset class and day."""

    def __init__(self, conn, asset_class, day, deps, run_id, ai: bool = True):
        self.conn, self.ac, self.day, self.deps = conn, asset_class, day, deps
        self.crypto = asset_class == "crypto"
        self.hb = Heartbeat(conn, run_id, deps.now)
        self.report = RunReport(run_id, asset_class, day)
        self.rule_ids = roster.rule_ids(asset_class)
        self.ai_ids = roster.ai_ids(asset_class) if ai else ()
        self.ids = self.rule_ids + self.ai_ids
        for aid in self.rule_ids:
            self.report.agents[aid] = AgentReport(aid, "rule")
        for aid in self.ai_ids:
            self.report.agents[aid] = AgentReport(aid, "ai")

    # -- steps --

    def data(self, exp: ex.Experiment) -> datetime:
        """Steps 0-1: fill bars (crypto) and daily bars, unless the day is already fully stored."""
        all_settled = all(is_settled(self.conn, a, self.day) for a in self.ids)
        done = all_settled and stored_snapshot(self.conn, self.ac, self.day) is not None
        if self.crypto and self.day > exp.start_date:            # D-1's orders fill at these bars
            prev = self.day - timedelta(days=1)
            if all_settled:
                self.hb("fetch_fill_prices", "finished", skipped="every crypto agent already settled",
                        date=prev.isoformat())
            elif self.deps.fill_prices is None:
                self.hb("fetch_fill_prices", "error", error="no fill_prices dependency")
            else:
                starts = crypto_window_starts(self.conn, self.ids, prev) or [None]
                for start in starts:
                    ws = None if start is None else {prev: start}
                    if self.deps.fill_prices(self.conn, [prev], self.hb.run_id, self.deps.now, ws) != 0:
                        self.hb.errors.append("fetch_fill_prices: failed (see its heartbeat)")
        if done:
            self.hb("fetch_prices", "finished", skipped="snapshot stored and every agent settled")
        elif self.deps.ingest is None:
            self.hb("fetch_prices", "error", error="no ingest dependency")
        else:
            code = self.deps.ingest(self.conn, self.ac, self.hb.run_id, self.deps.now)
            if code != 0:
                self.hb.errors.append("fetch_prices / data_checks: failed (see their heartbeat)")
        return self.deps.now()

    def settle(self) -> set[str]:
        self.hb("settle", "started", agents=list(self.ids))
        settled = set()
        for aid in self.ids:
            rep = self.report.agents[aid]
            try:
                res = LD.settle_day(self.conn, aid, self.day, self.deps.now(), mode="live", trading=self.deps.trading)
            except Exception as exc:  # noqa: BLE001 - one agent's failure doesn't stop the others
                rep.settle = "error"
                self.hb(f"settle:{aid}", "error", agent_id=aid, error=_err(exc))
                continue
            settled.add(aid)
            rep.settle = res.status
            eq = self.conn.execute("SELECT equity FROM equity WHERE agent_id = ? AND as_of_date = ? "
                                   "ORDER BY equity_id DESC LIMIT 1", (aid, self.day.isoformat())).fetchone()
            rep.equity = eq[0] if eq else None
            self.hb(f"settle:{aid}", "finished", agent_id=aid, status=res.status, fills=len(res.fills),
                    equity=rep.equity, cash_floor_events=len(res.floor_events),
                    **({"cancelled": list(res.cancelled)} if res.cancelled else {}))
        self.hb("settle", "finished", settled=sorted(settled, key=_order),
                failed=sorted(set(self.ids) - settled, key=_order))
        return settled

    def snapshot(self, times: cal.DailyTimes, data_done_at: datetime, deadline: datetime):
        """Step 3. Returns (Snapshot, snapshot_id, checks_passed) or (None, None, False)."""
        row = stored_snapshot(self.conn, self.ac, self.day)
        try:
            if row is not None:
                payload = json.loads(row["payload"])
                cutoff = cal._parse_ts(row["cutoff_ts"]) if payload.get("late") else None
                snap = sn.build_snapshot(self.conn, self.ac, self.day, "live", trading=self.deps.trading,
                                         now=self.deps.now(), cutoff=cutoff)
                if snap.sha256 != row["sha256"]:
                    raise sn.SnapshotError(f"rebuilt snapshot hash {snap.sha256} differs from stored "
                                           f"{row['sha256']} (snapshot {row['snapshot_id']})")
                self.hb("build_snapshot", "finished", skipped="already stored", snapshot_id=row["snapshot_id"],
                        sha256=row["sha256"], checks_passed=bool(row["checks_passed"]))
                self._late(payload)
                return snap, row["snapshot_id"], bool(row["checks_passed"])
            if self.deps.now() > deadline:
                self.hb("build_snapshot", "error", reason="past the decision deadline: no snapshot, no decisions",
                        deadline=db.utc_ts(deadline))
                return None, None, False
            late = data_done_at > times.snapshot_at
            if late:
                cutoff = cal._parse_ts(db.utc_ts(self.deps.now()))
            else:
                wait_until(times.snapshot_at, self.deps)
                cutoff = None
            snap = sn.build_snapshot(self.conn, self.ac, self.day, "live", trading=self.deps.trading,
                                     now=self.deps.now(), cutoff=cutoff)
            payload = snap.payload
            if payload["late"]:
                self.hb("late_run", "finished", minutes_late=payload["minutes_late"], cutoff_ts=snap.cutoff_ts,
                        protocol_cutoff=db.utc_ts(times.snapshot_at), deadline=db.utc_ts(deadline))
            sid = sn.store_snapshot(self.conn, snap, trading=self.deps.trading, now=self.deps.now(),
                                    run_id=self.hb.run_id)
            passed = bool(self.conn.execute("SELECT checks_passed FROM snapshots WHERE snapshot_id = ?",
                                            (sid,)).fetchone()[0])
            self._late(payload)
            return snap, sid, passed
        except Exception as exc:  # noqa: BLE001
            self.hb("build_snapshot", "error", error=_err(exc))
            return None, None, False

    def _late(self, payload):
        self.report.late = bool(payload.get("late"))
        self.report.minutes_late = int(payload.get("minutes_late", 0))

    def margin_only(self, settled: set[str], window: tuple[datetime, datetime], deadline: datetime, why: str):
        """No decisions today: each settled agent still gets the margin check (§7 rule 8), before the deadline."""
        for aid in sorted(settled, key=_order):
            rep = self.report.agents[aid]
            rep.decision = "not decided"
            rep.reason = why
            if self.deps.now() > deadline:
                rep.placed = "past deadline"
                continue
            if already_placed(self.conn, aid, *window):
                rep.placed = "already_placed"
                continue
            try:
                acct = LD.load_account(self.conn, aid, as_of=self.day)
                bars = LD.day_bars(self.conn, sorted(acct.positions), self.day, self.deps.now())
                closes = {s: bars[s]["close"] for s in acct.positions}
                placed = LD.place_orders(self.conn, aid, self.day, closes, None, self.deps.now())
            except Exception as exc:  # noqa: BLE001
                self.hb(f"place_orders:{aid}", "error", agent_id=aid, error=_err(exc))
                continue
            rep.placed, rep.orders = placed.status, placed.orders
            self.hb(f"place_orders:{aid}", "finished", agent_id=aid, status=placed.status, hold=True,
                    orders=len(placed.orders), margin_events=len(placed.margin_events))

    def decide(self, snap, sid: int, settled: set[str], exp: ex.Experiment, times: cal.DailyTimes,
               deadline: datetime, window: tuple[datetime, datetime]) -> None:
        """Steps 5-8."""
        closes = {s: f["last_close"] for s, f in snap.payload["symbols"].items() if f["last_close"] is not None}
        self.eligible = [] if self.crypto else ret63_top(snap)   # agent 7's list (v1.15), as in its prompt
        ctx0 = build_context(self.day, run_sessions(self.conn, self.ac, exp.start_date, self.day, self.deps.trading))
        todo, stored = [], []
        for aid in self.ids:
            if aid not in settled:
                self.report.agents[aid].decision = "not settled"
                continue
            if already_placed(self.conn, aid, *window):
                self.report.agents[aid].decision = "stored"
                self.report.agents[aid].placed = "already_placed"
                self._fill_from_store(aid, sid)
                continue
            (stored if final_decision(self.conn, aid, sid) is not None else todo).append(aid)
        if not self.report.late:
            wait_until(times.decide_at, self.deps)
        self.hb("decide", "started", to_decide=todo, stored=stored, ai_parallel=[a for a in todo if a in self.ai_ids])

        shadow = (roster.shadow_active(exp.name, self.ac, self.day) and roster.SHADOW_OF in settled
                  and not self.conn.execute("SELECT 1 FROM shadow_decisions WHERE shadow_of = ? AND snapshot_id = ?",
                                            (roster.SHADOW_OF, sid)).fetchone())
        etb: dict[str, bool] = {}
        if not self.crypto and (shadow or any(profile(a).shorts for a in todo + stored)):
            stocks = sorted(s for s in snap.payload["symbols"] if s != "SPY")
            self.hb("fetch_etb", "started", symbols=len(stocks))
            try:
                etb = dict(self.deps.etb(stocks)) if self.deps.etb else {}
                self.hb("fetch_etb", "finished", flags=etb)
            except Exception as exc:  # noqa: BLE001 - no flags: new shorts are rejected and logged
                self.hb("fetch_etb", "error", error=_err(exc), effect="no flags: new shorts rejected")

        run = self._run_info(exp)
        inputs = {aid: self._inputs(aid, closes, ctx0, run) for aid in todo + stored}
        if shadow and roster.SHADOW_OF not in inputs:
            inputs[roster.SHADOW_OF] = self._inputs(roster.SHADOW_OF, closes, ctx0, run)

        self.deferred: list[tuple[str, int, Any]] = []      # crypto: placed together at t_ref
        for aid in stored:                                     # decided in an earlier run, not placed yet
            row = final_decision(self.conn, aid, sid)
            self.report.agents[aid].decision = f"stored ({row['status']})"
            weights = applied_weights(self.conn, row)
            if aid in ELIGIBLE_AGENTS:       # the same sale as when it was recorded; its events are stored
                kind = row["status"] if row["status"] != "ok" else "hold" if weights is None else "rebalance"
                weights, _ = apply_eligibility(weights, inputs[aid][2], self.eligible, kind)
            self._queue_place(aid, row["decision_id"], weights, closes, deadline)

        ai_todo = [a for a in todo if a in self.ai_ids and a != BLENDER_ID]
        blender = BLENDER_ID in todo                           # after agents 3-6 (§5 v1.16)
        pool = ThreadPoolExecutor(max_workers=max(1, len(ai_todo) + blender), thread_name_prefix="ai")
        futures: dict[Future, str] = {}
        try:
            for aid in ai_todo:
                agent = roster.ai_agent(aid, self.deps.client_for(aid), self.deps.ai_sleep, self.deps.ai_backoff)
                portfolio, ctx, _ = inputs[aid]
                self.hb(f"decide:{aid}", "started", agent_id=aid, model=agent.model.model_id)
                futures[pool.submit(agent.decide, snap, portfolio, ctx)] = aid
            if shadow:                                         # §8 gate: untraded, agent 8's exact inputs
                agent = roster.shadow_agent(self.deps.client_for(roster.SHADOW_KEY), self.deps.ai_sleep,
                                            self.deps.ai_backoff)
                portfolio, ctx, _ = inputs[roster.SHADOW_OF]
                self.hb(f"decide:{roster.SHADOW_KEY}", "started", shadow_of=roster.SHADOW_OF,
                        model=agent.model.model_id, reasoning=agent.model.reasoning)
                futures[pool.submit(agent.decide, snap, portfolio, ctx)] = roster.SHADOW_KEY
            for aid in todo:                                   # rule agents, in turn, while the AI calls run
                if aid in self.ai_ids:
                    continue
                portfolio, ctx, current = inputs[aid]
                self.hb(f"decide:{aid}", "started", agent_id=aid)
                try:
                    dec, error = roster.rule_agent(aid)(snap, portfolio, ctx), None
                except Exception as exc:  # noqa: BLE001 - a crash is invalid output: hold
                    dec, error = Decision.hold(f"held (invalid): {_err(exc)}"[:300]), _err(exc)
                self._record(aid, sid, dec, error, current, etb, closes, deadline)
            plan = None
            if blender:
                plan = self._blend_plan(sid, closes, ctx0, inputs)
                if plan["ai"]:
                    portfolio, ctx, _ = inputs[BLENDER_ID]
                    ctx = replace(ctx, memory=replace(ctx.memory, strategies=plan["strategies"],
                                                      allocation=plan["allocation"]))
                    agent = roster.ai_agent(BLENDER_ID, self.deps.client_for(BLENDER_ID), self.deps.ai_sleep,
                                            self.deps.ai_backoff)
                    self.hb(f"decide:{BLENDER_ID}", "started", agent_id=BLENDER_ID, model=agent.model.model_id,
                            allocation=plan["allocation"], traded=plan["traded"])
                    futures[pool.submit(agent.decide, snap, portfolio, ctx)] = BLENDER_ID
                else:
                    self.hb(f"decide:{BLENDER_ID}", "started", agent_id=BLENDER_ID, ai_call=False,
                            allocation=plan["allocation"], traded=plan["traded"])
                    self._record_blender(sid, None, None, plan, inputs[BLENDER_ID][2], etb, closes, deadline)
            for fut in as_completed(futures):
                aid = futures[fut]
                try:
                    dec, error = fut.result(), None
                except Exception as exc:  # noqa: BLE001
                    dec, error = Decision.hold(f"held (invalid): {_err(exc)}"[:300]), _err(exc) + \
                        "\n" + traceback.format_exc()[-1500:]
                if aid == roster.SHADOW_KEY:
                    self._record_shadow(sid, dec, error, inputs[roster.SHADOW_OF][2], etb, deadline)
                    continue
                if aid == BLENDER_ID:
                    self._record_blender(sid, dec, error, plan, inputs[aid][2], etb, closes, deadline)
                    continue
                self._record(aid, sid, dec, error, inputs[aid][2], etb, closes, deadline)
        finally:
            pool.shutdown(wait=True)
        if self.crypto and self.deferred:
            self._place_crypto(closes, deadline, window)
        self.report.decided = True
        self.hb("decide", "finished", decisions={a: self.report.agents[a].decision for a in todo + stored})

    def _run_info(self, exp: ex.Experiment) -> tuple[date | None, int | None]:
        """input.run (v1.15): the label's end date and the run's trading days after today.

        (None, None) if the label has no end date or today is after it: the AI agents then fail
        their prompt and hold as invalid; a heartbeat says why. Rule agents are unaffected."""
        try:
            left = trading_days_left(self.conn, self.ac, self.day, exp.end_date, self.deps.trading)
        except Exception as exc:  # noqa: BLE001
            self.hb("run_info", "error", error=_err(exc), effect="AI agents hold (invalid)")
            return None, None
        self.hb("run_info", "finished", end_date=exp.end_date.isoformat(), trading_days_left=left)
        return exp.end_date, left

    def _inputs(self, aid: str, closes: Mapping[str, float], ctx0, run=(None, None)):
        acct = LD.load_account(self.conn, aid, as_of=self.day)
        pv = acct.value(closes)
        portfolio = Portfolio(cash=acct.cash, positions={s: p.qty for s, p in acct.positions.items()}, value=pv)
        current = {s: p.qty * closes[s] / pv for s, p in acct.positions.items()} if pv else {}
        ctx = ctx0
        if aid in self.ai_ids:
            ctx = replace(ctx0, memory=AgentMemory(
                starting_value=L.START_CASH, avg_cost={s: p.avg_cost for s, p in acct.positions.items()},
                last_decisions=past_decisions(self.conn, aid, self.ac, self.day),
                end_date=run[0], trading_days_left=run[1]))
        return portfolio, ctx, current

    def _record(self, aid, sid, dec, error, current, etb, closes, deadline) -> None:
        """Step 7 (+8): decision rows and risk events in one transaction, then the orders."""
        rep = self.report.agents[aid]
        record = getattr(dec, "record", None)
        rep.reason = dec.reason
        rep.asked = None if dec.weights is None else dict(dec.weights)
        decided_at = self.deps.now()
        if decided_at > deadline:
            rep.decision = "past deadline"
            self.hb(f"decide:{aid}", "error", agent_id=aid, reason="decided after the deadline: not recorded, holds",
                    deadline=db.utc_ts(deadline))
            return
        weights, events = (None, []) if error else apply_risk(aid, dec, etb, current)
        if aid in ELIGIBLE_AGENTS:           # §5 v1.15: a held stock off the list is sold on any day
            status = "invalid" if error else record.status if record is not None else "ok"
            kind = status if status != "ok" else "hold" if weights is None else "rebalance"
            weights, sold = apply_eligibility(weights, current, self.eligible, kind)
            events = events + sold
        ts = db.utc_ts(decided_at)
        with self.conn:
            if record is not None:
                did = None
                for a in record.attempts:
                    did = _insert(self.conn, "decisions", {"agent_id": aid, "snapshot_id": sid,
                                                           "decided_at": ts, **a.row()})
                status = record.status
                rep.attempts = len(record.attempts)
                rep.cost_usd = sum(a.cost_usd or 0.0 for a in record.attempts)
            else:
                status = "invalid" if error else "ok"
                row = {"agent_id": aid, "snapshot_id": sid, "decided_at": ts, "status": status,
                       "parsed_targets": None if error else json.dumps(None if dec.weights is None
                                                                       else dict(dec.weights), sort_keys=True),
                       "overall_reasoning": dec.reason, "error": error}
                did = _insert(self.conn, "decisions", row)
                rep.attempts = 1
            LD.write_risk_events(self.conn, aid, events, decided_at, did)
        rep.decision, rep.risk_events = status, len(events)
        # an outage or the spend cap is an operational error; invalid model output is data (§8)
        failed = error is not None or status in ("unavailable", "capped")
        self.hb(f"decide:{aid}", "error" if failed else "finished", agent_id=aid, status=status, decision_id=did,
                attempts=rep.attempts, cost_usd=rep.cost_usd, hold=dec.weights is None, risk_events=len(events),
                **({"error": error[:500]} if error else {"reason": f"held ({status})"} if failed else {}))
        self._queue_place(aid, did, weights, closes, deadline)

    # -- agent 13, the blender (§5 v1.16) --

    def _weights_after_decision(self, k: str, sid: int, closes: Mapping[str, float]) -> dict[str, float]:
        """w_k: agent k's risk-checked targets if it rebalanced today, else its weights at the close."""
        row = final_decision(self.conn, k, sid)
        w = applied_weights(self.conn, row) if row is not None else None
        if w is not None:
            return {s: x for s, x in sorted(w.items()) if x}
        acct = LD.load_account(self.conn, k, as_of=self.day)
        pv = acct.value(closes)
        return {s: p.qty * closes[s] / pv for s, p in sorted(acct.positions.items()) if pv and p.qty}

    def _blend_plan(self, sid: int, closes: Mapping[str, float], ctx0, inputs) -> dict:
        """Agent 13's day: the strategies it sees and blends, its allocation, whether it calls the AI."""
        strategies, traded = {}, []
        for k in BLEND_OF:
            row = final_decision(self.conn, k, sid)
            if row is not None and row["status"] == "ok" and json.loads(row["parsed_targets"]) is not None:
                traded.append(k)                               # a non-hold decision today
            strategies[k] = {"weights": self._weights_after_decision(k, sid, closes),
                             **BL.strategy_stats(BL.marks_up_to(self.conn, k, self.day))}
        allocation = BL.current_allocation(self.conn, self.day)
        ai = ctx0.is_first_session_of_week or allocation is None
        return {"strategies": strategies, "allocation": allocation, "traded": traded, "ai": ai}

    def _record_blender(self, sid, dec, error, plan, current, etb, closes, deadline) -> None:
        """Agent 13's rows: its AI call (if any), then a follow row when it didn't set an allocation
        but agents 3-6 traded and it has one; or a hold row with no call. Risk events on the placed row."""
        aid, rep = BLENDER_ID, self.report.agents[BLENDER_ID]
        record = getattr(dec, "record", None)
        decided_at = self.deps.now()
        if decided_at > deadline:
            rep.decision = "past deadline"
            self.hb(f"decide:{aid}", "error", agent_id=aid, reason="decided after the deadline: not recorded, holds",
                    deadline=db.utc_ts(deadline))
            return
        ts = db.utc_ts(decided_at)
        alloc, traded = plan["allocation"], plan["traded"]
        allocated = error is None and dec is not None and dec.weights is not None
        follow = not allocated and alloc is not None and bool(traded)
        rows: list[dict] = []
        if record is not None:
            rows += [a.row() for a in record.attempts]
        elif plan["ai"]:                                       # the call crashed: invalid, like any agent
            rows.append({"attempt": 1, "status": "invalid", "parsed_targets": None,
                         "overall_reasoning": dec.reason if dec is not None else None, "error": error})
        status = record.status if record is not None else "invalid" if plan["ai"] else "ok"
        share = ", ".join(f"{k}: {alloc[k]:.2f}" for k in BLEND_OF) if alloc else ""
        weights = None
        if allocated:
            weights = dict(dec.weights)
        elif follow:
            weights = blend(alloc, plan["strategies"])
            why = ("no new allocation today" if plan["ai"] else "not a decision day")
            rows.append({"attempt": len(rows) + 1, "status": "ok", "parsed_targets": json.dumps(weights, sort_keys=True),
                         "parsed_allocation": json.dumps(alloc, sort_keys=True),
                         "overall_reasoning": f"Follows agents {', '.join(traded)}, which traded today, at the "
                                              f"unchanged allocation ({share}); {why}"})
        elif not plan["ai"]:
            rows.append({"attempt": 1, "status": "ok", "parsed_targets": "null",
                         "parsed_allocation": None if alloc is None else json.dumps(alloc, sort_keys=True),
                         "overall_reasoning": "Not a decision day and agents 3-6 did not trade: hold"})
        events: list[dict] = []
        if weights is not None:
            weights, events = apply_risk(aid, Decision(weights, "blend"), etb, current)
        with self.conn:
            did = None
            for r in rows:
                did = _insert(self.conn, "decisions", {"agent_id": aid, "snapshot_id": sid, "decided_at": ts, **r})
            LD.write_risk_events(self.conn, aid, events, decided_at, did)
        rep.attempts = len(record.attempts) if record is not None else 0
        rep.cost_usd = sum(a.cost_usd or 0.0 for a in record.attempts) if record is not None else None
        rep.reason = dec.reason if allocated or (dec is not None and not follow) else rows[-1]["overall_reasoning"]
        rep.asked = None if weights is None else dict(weights)
        rep.decision = status + (" + follow" if follow else "") if plan["ai"] else ("follow" if follow else "hold")
        rep.risk_events = len(events)
        failed = error is not None or status in ("unavailable", "capped")
        self.hb(f"decide:{aid}", "error" if failed else "finished", agent_id=aid, status=status, decision_id=did,
                ai_call=plan["ai"], attempts=rep.attempts, cost_usd=rep.cost_usd, follow=follow,
                allocation=dict(record.attempts[-1].parsed.allocation) if allocated else alloc,
                hold=weights is None, risk_events=len(events), **({"error": error[:500]} if error else {}))
        self._queue_place(aid, did, weights, closes, deadline)

    def _record_shadow(self, sid, dec, error, current, etb, deadline) -> None:
        """§8 gate (v1.14): the shadow call's attempts, with its targets after agent 8's risk checks.
        Never traded: no risk_events, no orders."""
        job = f"decide:{roster.SHADOW_KEY}"
        record = getattr(dec, "record", None)
        decided_at = self.deps.now()
        if decided_at > deadline:
            self.hb(job, "error", reason="decided after the deadline: not recorded")
            return
        if error is not None or record is None:
            self.hb(job, "error", error=(error or "no call record")[:500])
            return
        weights, _ = apply_risk(roster.SHADOW_OF, dec, etb, current)
        ts = db.utc_ts(decided_at)
        last = len(record.attempts) - 1
        with self.conn:
            for i, a in enumerate(record.attempts):
                row = a.row()
                row.pop("reasoning_setting", None)
                row.pop("parsed_allocation", None)              # agent 8's shadow: never an allocation
                _insert(self.conn, "shadow_decisions", {
                    "shadow_of": roster.SHADOW_OF, "snapshot_id": sid, "decided_at": ts,
                    "reasoning_setting": a.reasoning_setting, **row,
                    "applied_targets": (json.dumps(dict(weights), sort_keys=True)
                                        if i == last and weights is not None else None)})
        cost = sum(a.cost_usd or 0.0 for a in record.attempts)
        failed = record.status in ("unavailable", "capped")
        self.hb(job, "error" if failed else "finished", status=record.status, attempts=len(record.attempts),
                cost_usd=cost, reasoning_tokens=record.attempts[-1].reasoning_tokens, hold=weights is None)

    def _queue_place(self, aid, did, weights, closes, deadline) -> None:
        if self.crypto:
            self.report.agents[aid].applied = weights
            self.deferred.append((aid, did, weights))
        else:
            self._place(aid, did, weights, closes, deadline)

    def _place_crypto(self, closes, deadline, window) -> None:
        """§7 rule 4 (v1.13): every crypto agent's orders at one t_ref, the last decision's record time."""
        names = ", ".join("?" * len(self.deferred))
        last = self.conn.execute(f"SELECT MAX(decided_at) FROM decisions WHERE decision_id IN ({names})",
                                 [did for _, did, _ in self.deferred]).fetchone()[0]
        t_ref = cal._parse_ts(last)
        placed_at = [r[0] for r in self.conn.execute(
            f"""SELECT DISTINCT created_at FROM orders WHERE venue = 'ledger' AND agent_id IN
                ({", ".join("?" * len(self.ids))}) AND created_at >= ? AND created_at < ? ORDER BY created_at""",
            [*self.ids, db.utc_ts(window[0]), db.utc_ts(window[1])])]
        if placed_at:
            # The day's t_ref is already fixed by orders an earlier run placed. An agent that decided
            # after it is missed for the day (no order, labelled late), never a second t_ref; one that
            # decided by then (its placement failed earlier) is placed at the day's t_ref.
            day_ref = placed_at[0]
            decided = {did: r[0] for did in (d for _, d, _ in self.deferred)
                       for r in [self.conn.execute("SELECT decided_at FROM decisions WHERE decision_id = ?",
                                                   (did,)).fetchone()]}
            missed = [a for a, did, _ in self.deferred if decided[did] > day_ref]
            self.hb("crypto_t_ref", "finished", t_ref=day_ref, fixed_by="orders placed by an earlier run",
                    missed=missed)
            for aid, did, weights in self.deferred:
                if aid in missed:
                    self.report.agents[aid].placed = "missed"
                    self.hb(f"place_orders:{aid}", "error", agent_id=aid, status="missed", late=True,
                            t_ref=day_ref, decided_at=decided[did],
                            reason="decided after the day's crypto orders were placed: no trade today "
                                   "(§7 rule 4, v1.13)")
                else:
                    self._place(aid, did, weights, closes, deadline, created_at=cal._parse_ts(day_ref))
            return
        self.hb("crypto_t_ref", "finished", t_ref=last, fill_from=db.utc_ts(LD.ceil_minute(t_ref)),
                agents=[a for a, _, _ in self.deferred])
        for aid, did, weights in self.deferred:
            self._place(aid, did, weights, closes, deadline, created_at=t_ref)

    def _place(self, aid, did, weights, closes, deadline, created_at: datetime | None = None) -> None:
        rep = self.report.agents[aid]
        rep.applied = weights
        if self.deps.now() > deadline:
            rep.placed = "past deadline"
            self.hb(f"place_orders:{aid}", "error", agent_id=aid, reason="past the deadline: nothing placed")
            return
        try:
            placed = LD.place_orders(self.conn, aid, self.day, closes, weights, created_at or self.deps.now(),
                                     did if weights is not None else None)
        except Exception as exc:  # noqa: BLE001
            self.hb(f"place_orders:{aid}", "error", agent_id=aid, error=_err(exc))
            return
        rep.placed, rep.orders = placed.status, placed.orders
        if placed.status == "missed":
            self.hb(f"place_orders:{aid}", "error", agent_id=aid, status="missed", late=True,
                    reason="decided after the next day's snapshot cutoff: no trade (§7 rule 4, v1.13)")
            return
        self.hb(f"place_orders:{aid}", "finished", agent_id=aid, status=placed.status, orders=len(placed.orders),
                margin_events=len(placed.margin_events), margin_superseded=placed.margin_superseded)

    def _fill_from_store(self, aid, sid) -> None:
        row = final_decision(self.conn, aid, sid) if sid is not None else None
        rep = self.report.agents[aid]
        if row is not None:
            rep.decision = f"stored ({row['status']})"
            rep.asked = json.loads(row["parsed_targets"]) if row["parsed_targets"] else None
            rep.applied = applied_weights(self.conn, row)
            rep.reason = row["overall_reasoning"] or row["error"] or ""


def _order(aid: str) -> tuple:
    return (aid.startswith("C"), int(aid.lstrip("C")))


def run_day(conn: sqlite3.Connection, asset_class: str, day: date | None = None, deps: Deps | None = None,
            run_id: str | None = None, ai: bool = True) -> RunReport:
    """Run (or resume) one day. Never raises for a step's failure: it is logged and reported.

    ai=False (--no-ai, dev labels only): the rule-based agents (1-6, C0) only. The AI agents are
    skipped like unbuilt ones (no rows, no client created), so the run costs nothing."""
    deps = deps or Deps()
    if asset_class not in ASSET_CLASSES.values():
        raise ValueError(f"unknown asset class {asset_class!r}")
    run_id = run_id or uuid.uuid4().hex
    hb = Heartbeat(conn, run_id, deps.now)
    try:
        day = day or default_date(asset_class, deps.now(), conn, deps.trading)
        exp = ex.require(conn, day)
    except Exception as exc:
        hb(JOB, "error", asset_class=asset_class, date=None if day is None else day.isoformat(), error=_err(exc))
        raise
    if not ai and exp.name != NO_AI_LABEL:
        hb(JOB, "error", asset_class=asset_class, date=day.isoformat(),
           error=f"--no-ai is only allowed on a {NO_AI_LABEL!r} label, not {exp.name!r}")
        raise ex.ExperimentError(f"--no-ai is only allowed on a {NO_AI_LABEL!r} label, not {exp.name!r}")
    d = _Day(conn, asset_class, day, deps, run_id, ai)
    hb = d.hb
    rep = d.report
    rep.skipped = roster.not_built(asset_class) + (() if ai else roster.ai_ids(asset_class))
    hb(JOB, "started", asset_class=asset_class, date=day.isoformat(), experiment=exp.name,
       skipped_not_built=list(roster.not_built(asset_class)), ai=ai,
       **({} if ai else {"skipped_no_ai": list(roster.ai_ids(asset_class))}))
    for aid in roster.not_built(asset_class):
        print(f"agent {aid} not built yet: skipped")
    if not ai:
        print(f"--no-ai: AI agents {', '.join(roster.ai_ids(asset_class))} skipped")
    try:
        if asset_class == "us_equity":
            if cal.session(day, conn, deps.trading) is None:
                rep.status = "market_closed"
                hb(JOB, "finished", status="market_closed", date=day.isoformat())
                return rep
            times = cal.equity_times(day, conn, deps.trading)
        else:
            times = cal.crypto_times(day)
        deadline = cal.decision_deadline(asset_class, day, conn, deps.trading)
        window = (times.snapshot_at, deadline + cal.LATE_MARGIN)

        wait_until(times.bars_fetch_at, deps)
        data_done_at = d.data(exp)
        settled = d.settle()
        snap, sid, passed = d.snapshot(times, data_done_at, deadline)
        rep.snapshot_id, rep.checks_passed = sid, passed if sid is not None else None
        if snap is None or not passed or deps.now() > deadline:
            why = ("past the decision deadline" if deps.now() > deadline else
                   "no snapshot" if snap is None else "snapshot failed its data checks")
            hb("decide", "error", reason=f"{why}: no agent decides today", snapshot_id=sid,
               deadline=db.utc_ts(deadline))
            for aid in d.ids:
                if sid is not None:
                    d._fill_from_store(aid, sid)
            d.margin_only(settled, window, deadline, why)
        else:
            d.decide(snap, sid, settled, exp, times, deadline, window)
    except Exception as exc:  # noqa: BLE001 - an unexpected failure still ends with an error heartbeat
        hb(JOB, "error", error=_err(exc), trace=traceback.format_exc()[-2000:])
        rep.errors = list(hb.errors)
        rep.status = "error"
        return rep
    rep.errors = list(hb.errors)
    rep.status = "error" if rep.errors else "ok"
    hb(JOB, "error" if rep.errors else "finished", status=rep.status, errors=rep.errors, late=rep.late,
       minutes_late=rep.minutes_late, snapshot_id=sid, checks_passed=rep.checks_passed,
       agents={a: {"decision": r.decision, "orders": len(r.orders), "placed": r.placed}
               for a, r in rep.agents.items()})
    return rep


# ---------------------------------------------------------------------------
# Output and CLI
# ---------------------------------------------------------------------------

def _w(w: Mapping[str, float] | None) -> str:
    if w is None:
        return "hold"
    shown = [(s, x) for s, x in sorted(w.items(), key=lambda kv: (-abs(kv[1]), kv[0])) if x]
    return ", ".join(f"{s} {x:+.1%}" for s, x in shown) or "all cash"


def format_report(r: RunReport) -> str:
    lines = [f"run {r.run_id}: {r.asset_class} {r.day}  status {r.status}"
             + (f"  LATE by {r.minutes_late} min" if r.late else "")
             + f"  snapshot {r.snapshot_id} checks_passed={r.checks_passed}"]
    if r.skipped:
        lines.append("skipped (not built, or --no-ai): " + ", ".join(r.skipped))
    for aid in sorted(r.agents, key=_order):
        a = r.agents[aid]
        cost = "" if a.cost_usd is None else f"  ${a.cost_usd:.4f}"
        eq = "" if a.equity is None else f"  equity {a.equity:,.2f}"
        lines.append(f"  {aid:>3} {a.kind:<4} settle={a.settle or '-'}{eq}  decision={a.decision or '-'}"
                     f" ({a.attempts} call{'s' if a.attempts != 1 else ''}){cost}  placed={a.placed or '-'}"
                     f" orders={len(a.orders)} risk_events={a.risk_events}")
        lines.append(f"        asked: {_w(a.asked)}")
        if a.asked is not None:
            lines.append(f"        after risk: {_w(a.applied)}")
        if a.reason:
            lines.append(f"        reason: {a.reason[:240]}")
    for e in r.errors:
        lines.append(f"  ERROR {e}")
    return "\n".join(lines)


class RunLocked(Exception):
    pass


def lock(path: Path, asset_class: str):
    f = open(f"{path}.{asset_class}.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise RunLocked(f"another {asset_class} run holds the lock on {path}") from None
    return f


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run one trading day (protocol §3, §13).")
    p.add_argument("asset", choices=sorted(ASSET_CLASSES))
    p.add_argument("--date", type=date.fromisoformat, default=None, metavar="YYYY-MM-DD",
                   help="default: the latest closed day (data/calendar.py)")
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--experiment", help="shakedown | official | ... (data/<name>.db)")
    where.add_argument("--db", type=Path, help="database path (e.g. a temp copy)")
    args = p.parse_args(argv)
    path = args.db or ex.path_for(args.experiment)
    if not path.exists():
        print(f"ERROR: {path} not found (create it with python -m runner.experiment create)", file=sys.stderr)
        return 1
    asset_class = ASSET_CLASSES[args.asset]
    try:
        held = lock(path, asset_class)
    except RunLocked as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    conn = db.init_db(path)
    try:
        exp = ex.require(conn)
        if args.experiment and exp.name != args.experiment:
            raise ex.ExperimentError(f"{path} is labelled {exp.name!r}, not {args.experiment!r}")
        report = run_day(conn, asset_class, args.date, real_deps())
    except (ex.ExperimentError, cal.CalendarError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
        held.close()
    print(format_report(report))
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
