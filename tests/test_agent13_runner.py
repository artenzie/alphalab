"""Agent 13 in the daily runner (§5, v1.16): when it calls the AI, the blend, follow and hold rows,
restarts, and its strategy inputs from marks up to the decision date only (rule 1)."""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agents.harness import BLEND_OF, blend  # noqa: E402
from data import calendar as cal  # noqa: E402
from data import db  # noqa: E402
from execution.risk import apply_risk  # noqa: E402
from runner import blender as BL  # noqa: E402
from runner import daily, roster  # noqa: E402
from strategies.base import Decision  # noqa: E402

import test_runner as TR  # noqa: E402
from test_runner import BLEND_ALLOC, DAYS, START, conn, equity_run  # noqa: E402,F401  (conn is a fixture)

HOLD = '{"action": "hold", "reasoning": "keep"}'


class Blender(TR.Client):
    """Agent 13 with a scripted reply per call (default: allocate BLEND_ALLOC)."""

    def __init__(self, replies=()):
        super().__init__("13")
        self.replies = list(replies)

    def complete(self, prompt, *, model):
        self.calls.append(prompt)
        text = self.replies.pop(0) if self.replies else json.dumps(
            {"action": "allocate", "allocation": BLEND_ALLOC, "reasoning": "blend", "confidence": 0.5})
        return TR.reply(model, {}, text=text)


def make_clients(blender=None):
    c = {aid: TR.Client(aid) for aid in roster.EQUITY_AI + roster.CRYPTO_AI}
    c["13"] = blender or Blender()
    return c


def rows13(conn, d):
    return conn.execute("""SELECT d.* FROM decisions d JOIN snapshots s USING (snapshot_id)
                           WHERE d.agent_id = '13' AND s.snapshot_date = ? ORDER BY attempt""",
                        (d.isoformat(),)).fetchall()


def sid_of(conn, d):
    return conn.execute("SELECT snapshot_id FROM snapshots WHERE snapshot_date = ? AND asset_class = 'us_equity'",
                        (d.isoformat(),)).fetchone()[0]


def strategy_weights(conn, d):
    """w_k as the runner defines it, recomputed independently: risk-checked targets, else close weights."""
    sid = sid_of(conn, d)
    out, traded = {}, []
    for k in BLEND_OF:
        row = daily.final_decision(conn, k, sid)
        w = daily.applied_weights(conn, row)
        if w is not None:
            traded.append(k)
            out[k] = {"weights": {s: x for s, x in w.items() if x}}
        else:
            acct = TR.LD.load_account(conn, k, as_of=d)
            closes = {s: TR.eq_close(s, d) for s in acct.positions}
            pv = acct.value(closes)
            out[k] = {"weights": {s: p.qty * closes[s] / pv for s, p in acct.positions.items() if p.qty}}
    return out, traded


def risk_checked(book, conn, d):
    acct = TR.LD.load_account(conn, "13", as_of=d)
    closes = {s: TR.eq_close(s, d) for s in acct.positions}
    pv = acct.value(closes)
    current = {s: p.qty * closes[s] / pv for s, p in acct.positions.items()}
    w, _ = apply_risk("13", Decision(book, "x"), {s: True for s in TR.load_universe()}, current)
    return w


def test_week_of_blending(conn):
    clients = make_clients()
    for d in DAYS:
        r, _ = equity_run(conn, d, clients)
        assert r.exit_code == 0, (d, r.errors)
        rows = rows13(conn, d)
        assert len(rows) == 1, d
        row = rows[0]
        strategies, traded = strategy_weights(conn, d)
        if d in (DAYS[0], DAYS[-1]):                       # Mon 16 (day 1) and Mon 23: an AI call
            assert row["model_id"] is not None and row["status"] == "ok"
            assert json.loads(row["parsed_allocation"]) == BLEND_ALLOC
            book = json.loads(row["parsed_targets"])
            assert book == pytest.approx(blend(BLEND_ALLOC, strategies), abs=1e-12)
        else:                                              # Tue-Fri: no call
            assert row["model_id"] is None and row["status"] == "ok"
            assert json.loads(row["parsed_allocation"]) == BLEND_ALLOC
            if traded:
                assert row["overall_reasoning"].startswith(f"Follows agents {', '.join(traded)}")
                assert json.loads(row["parsed_targets"]) == pytest.approx(blend(BLEND_ALLOC, strategies), abs=1e-12)
            else:
                assert json.loads(row["parsed_targets"]) is None
        # the orders were sized from the book after agent 13's own §6 checks
        applied = daily.applied_weights(conn, row)
        if applied is not None:
            assert applied == pytest.approx(risk_checked(json.loads(row["parsed_targets"]), conn, d), abs=1e-12)
    assert len(clients["13"].calls) == 2
    # day 1 shows agent 13 the strategies after their own day-1 decisions, and no allocation yet
    inp = json.loads(clients["13"].calls[0].split("\nINPUT\n")[1])
    assert inp["allocation"] is None and set(inp["strategies"]) == set(BLEND_OF)
    assert inp["strategies"]["3"]["return_since_start"] == 0.0 and inp["strategies"]["3"]["ret_5"] is None
    # Mon 23: its allocation, and last decisions = its AI calls only (not the follow / hold days)
    inp = json.loads(clients["13"].calls[1].split("\nINPUT\n")[1])
    assert inp["allocation"] == BLEND_ALLOC
    assert [d["date"] for d in inp["last_decisions"]] == [DAYS[0].isoformat()]
    assert inp["last_decisions"][0]["allocation"] == BLEND_ALLOC
    assert inp["strategies"]["3"]["ret_5"] is not None            # 6 marks by then (16-23 Nov)
    # some day in the week was a follow day, so the test covers it
    assert any(r["overall_reasoning"].startswith("Follows") for d in DAYS[1:-1] for r in rows13(conn, d))


def test_no_allocation_yet_calls_daily_and_stays_in_cash(conn):
    clients = make_clients(Blender([HOLD, HOLD]))
    for d in DAYS[:3]:
        equity_run(conn, d, clients)
    assert len(clients["13"].calls) == 3                          # Mon (hold), Tue (hold), Wed (allocate)
    for d in DAYS[:2]:
        rows = rows13(conn, d)
        assert [r["model_id"] is not None for r in rows] == [True]            # no follow: no allocation
        assert json.loads(rows[0]["parsed_targets"]) is None
    assert conn.execute("SELECT COUNT(*) FROM orders o WHERE agent_id = '13' AND created_at < ?",
                        (db.utc_ts(TR.close_of(DAYS[2])),)).fetchone()[0] == 0      # all cash
    assert json.loads(rows13(conn, DAYS[2])[0]["parsed_allocation"]) == BLEND_ALLOC


def test_hold_on_a_decision_day_follows_strategies_that_traded(conn):
    clients = make_clients(Blender([json.dumps({"action": "allocate", "allocation": BLEND_ALLOC,
                                                "reasoning": "first", "confidence": 0.5}), HOLD]))
    for d in DAYS:
        equity_run(conn, d, clients)
    rows = rows13(conn, DAYS[-1])                                  # Mon 23: 3 and 6 trade weekly
    strategies, traded = strategy_weights(conn, DAYS[-1])
    assert "3" in traded and "6" in traded
    assert [(r["attempt"], r["model_id"] is not None, r["status"]) for r in rows] == [(1, True, "ok"), (2, False, "ok")]
    assert json.loads(rows[0]["parsed_targets"]) is None and rows[0]["parsed_allocation"] is None
    assert json.loads(rows[1]["parsed_allocation"]) == BLEND_ALLOC
    assert json.loads(rows[1]["parsed_targets"]) == pytest.approx(blend(BLEND_ALLOC, strategies), abs=1e-12)
    assert "no new allocation today" in rows[1]["overall_reasoning"]
    placed = conn.execute("SELECT DISTINCT decision_id FROM orders WHERE agent_id = '13' AND created_at >= ?",
                          (db.utc_ts(TR.close_of(DAYS[-1])),)).fetchall()
    assert [p[0] for p in placed] in ([rows[1]["decision_id"]], [])
    # the hold, not the follow row, is its last decision
    assert [p.allocation for p in daily.past_decisions(conn, "13", "us_equity", DAYS[-1] + timedelta(days=1))][-1] is None


def test_invalid_then_follow(conn):
    bad = json.dumps({"action": "allocate", "allocation": {"3": 0.9, "4": 0.1, "5": 0, "6": 0},
                      "reasoning": "x", "confidence": 0.5})
    clients = make_clients(Blender([json.dumps({"action": "allocate", "allocation": BLEND_ALLOC,
                                                "reasoning": "first", "confidence": 0.5}), bad, bad]))
    for d in DAYS:
        equity_run(conn, d, clients)
    rows = rows13(conn, DAYS[-1])
    assert [(r["status"], r["model_id"] is not None) for r in rows] == [("invalid", True), ("invalid", True),
                                                                        ("ok", False)]
    assert json.loads(rows[2]["parsed_allocation"]) == BLEND_ALLOC


def test_restart_places_the_stored_blend_without_another_call(conn, monkeypatch):
    clients = make_clients()
    real = TR.LD.place_orders

    def flaky(conn_, agent_id, *a, **kw):
        if agent_id == "13":
            raise RuntimeError("disk full")
        return real(conn_, agent_id, *a, **kw)
    monkeypatch.setattr(TR.LD, "place_orders", flaky)
    r, _ = equity_run(conn, DAYS[0], clients)
    assert r.exit_code == 1
    monkeypatch.setattr(TR.LD, "place_orders", real)
    r2, _ = equity_run(conn, DAYS[0], clients, at=cal.equity_times(DAYS[0], conn).decide_at + timedelta(minutes=5))
    assert r2.exit_code == 0, r2.errors
    assert r2.agents["13"].decision.startswith("stored") and len(clients["13"].calls) == 1
    orders = conn.execute("SELECT symbol, side, target_weight FROM orders WHERE agent_id = '13'").fetchall()
    book = json.loads(rows13(conn, DAYS[0])[0]["parsed_targets"])
    applied = daily.applied_weights(conn, rows13(conn, DAYS[0])[0])
    assert {o["symbol"] for o in orders} == {s for s, w in applied.items() if w} and book


# ---------------------------------------------------------------------------
# Strategy stats from marks, and rule 1
# ---------------------------------------------------------------------------

def test_strategy_stats_thresholds_and_values():
    assert BL.strategy_stats([]) == {"ret_5": None, "ret_21": None, "return_since_start": None,
                                     "vol_20": None, "max_drawdown": None}
    one = BL.strategy_stats([100_000.0])
    assert one["return_since_start"] == 0.0 and one["max_drawdown"] is None and one["ret_5"] is None
    m = [100_000.0, 110_000.0, 99_000.0, 104_500.0, 100_000.0, 120_000.0]
    s = BL.strategy_stats(m)
    assert s["ret_5"] == pytest.approx(0.2) and s["ret_21"] is None and s["vol_20"] is None
    assert s["max_drawdown"] == pytest.approx(0.1) and s["return_since_start"] == pytest.approx(0.2)
    assert BL.strategy_stats(m[:5])["ret_5"] is None
    up = [100_000.0 * 1.01 ** i for i in range(21)]
    v = BL.strategy_stats(up)
    assert v["vol_20"] == pytest.approx(0.0, abs=1e-9) and v["max_drawdown"] == 0.0
    assert BL.strategy_stats(up[:-1])["vol_20"] is None
    assert v["ret_21"] is None and BL.strategy_stats(up + [up[-1]])["ret_21"] == pytest.approx(1.01 ** 20 - 1, abs=1e-6)


def test_no_mark_after_the_decision_date_enters(conn):
    """Rule 1: marks dated after D, and corrections of later days, never reach agent 13's input for D."""
    clients = make_clients()
    for d in DAYS[:3]:
        equity_run(conn, d, clients)
    d = DAYS[1]
    before = {k: BL.strategy_stats(BL.marks_up_to(conn, k, d)) for k in BLEND_OF}
    marks = {k: BL.marks_up_to(conn, k, d) for k in BLEND_OF}
    assert all(len(v) == 2 for v in marks.values())               # 16 and 17 Nov only
    def mark(k, day, eq):
        old = conn.execute("SELECT MAX(equity_id) FROM equity WHERE agent_id = ? AND as_of_date = ?",
                           (k, day.isoformat())).fetchone()[0]
        conn.execute("INSERT INTO equity (agent_id, as_of_date, cash, equity, corrects_id) VALUES (?, ?, ?, ?, ?)",
                     (k, day.isoformat(), eq, eq, old))
    with conn:
        for k in BLEND_OF:
            mark(k, DAYS[2], 1.0)                                  # a correction of a later day's mark
            mark(k, DAYS[3], 5e9)                                  # marks of later days
            mark(k, DAYS[5] + timedelta(days=30), 1.0)
    after = {k: BL.strategy_stats(BL.marks_up_to(conn, k, d)) for k in BLEND_OF}
    assert after == before and {k: BL.marks_up_to(conn, k, d) for k in BLEND_OF} == marks
    # the runner's own query for D's input reads the same: run D again is a no-op, so check the plan directly
    day = daily._Day(conn, "us_equity", d, TR.make_deps(TR.Clock(cal.equity_times(d, conn).decide_at),
                                                       TR.Market(TR.Clock(cal.equity_times(d, conn).decide_at)),
                                                       clients), "test")
    sid = sid_of(conn, d)
    ctx0 = daily.build_context(d, daily.run_sessions(conn, "us_equity", START, d, TR.no_network))
    closes = {s: TR.eq_close(s, d) for s in TR.UNIVERSE}
    plan = day._blend_plan(sid, closes, ctx0, {})
    for k in BLEND_OF:
        stats = {x: plan["strategies"][k][x] for x in before[k]}
        assert stats == before[k], k
    # a correction of D itself (a newer row for the same day) does apply: it is still dated D
    with conn:
        mark("3", d, 90_000.0)
    assert BL.marks_up_to(conn, "3", d)[-1] == 90_000.0


def test_current_allocation_reads_only_days_before(conn):
    clients = make_clients()
    equity_run(conn, DAYS[0], clients)
    assert BL.current_allocation(conn, DAYS[0]) is None
    assert BL.current_allocation(conn, DAYS[1]) == BLEND_ALLOC
    assert BL.current_allocation(conn, date(2026, 11, 1)) is None
