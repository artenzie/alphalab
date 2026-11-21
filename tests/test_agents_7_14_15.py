"""Agent 7, Constrained Claude (§5, v1.15): input.eligible, validation, and the eligibility sale."""

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agents import harness  # noqa: E402
from agents.harness import AIAgent, OutputError, parse_and_validate  # noqa: E402
from agents.llm import FakeClient  # noqa: E402
from agents.models import model_for  # noqa: E402
from agents.templates import load as load_template  # noqa: E402
from data import calendar as cal  # noqa: E402
from execution.risk import apply_eligibility  # noqa: E402
from runner import daily, roster  # noqa: E402
from strategies import momentum  # noqa: E402
from strategies.base import ret63_top, stock_fields  # noqa: E402
from test_harness import HOLDING, STOCKS, context, resp  # noqa: E402
from test_harness import D as HD  # noqa: E402
from test_strategies import EMPTY, RUN, ctx, make_snapshot  # noqa: E402

import test_runner as TR  # noqa: E402
from test_runner import DAYS, conn, equity_run  # noqa: E402,F401  (conn is a fixture)


def rebal(targets):
    return json.dumps({"action": "rebalance", "targets": targets, "reasons": {s: "r" for s in targets},
                       "reasoning": "x", "confidence": 0.5})


# ---------------------------------------------------------------------------
# The list: exactly agent 3's ranking
# ---------------------------------------------------------------------------

def test_eligible_is_agent_3s_top_5():
    snap = make_snapshot(RUN[0])
    dec = momentum.decide(snap, EMPTY, ctx(RUN[0]))           # first session of the run: agent 3 trades
    assert ret63_top(snap) == list(dec.weights) and len(ret63_top(snap)) == 5
    assert harness.eligible_for(7, snap) == harness.eligible_for("7", snap) == ret63_top(snap)
    assert harness.eligible_for(8, snap) is None


def test_eligible_ties_and_missing_ret_63():
    class Snap:
        payload = {"symbols": {"SPY": {"ret_63": 9.0}, "AAPL": {"ret_63": 0.1}, "ZTS": {"ret_63": 0.3},
                               "ABT": {"ret_63": 0.3}, "XOM": {"ret_63": None}, "CVX": {"ret_63": -0.2}}}
    assert ret63_top(Snap()) == ["ABT", "ZTS", "AAPL", "CVX"]  # SPY out, ties by symbol, None left out


def test_prompt_has_eligible_for_agent_7_only():
    snap = make_snapshot(HD)
    p7 = harness.build_prompt(load_template(7), 7, snap, HOLDING, context())
    p8 = harness.build_prompt(load_template(8), 8, snap, HOLDING, context())
    assert json.loads(p7.split("\nINPUT\n")[1])["eligible"] == ret63_top(snap)
    assert "eligible" not in json.loads(p8.split("\nINPUT\n")[1])
    assert "A non-zero target on a stock outside input.eligible makes your whole reply invalid." in p7


# ---------------------------------------------------------------------------
# Validation: non-zero outside the list is invalid; exactly 0 is a sale
# ---------------------------------------------------------------------------

def test_validation():
    ok = ["AAPL", "NVDA"]
    out = next(s for s in STOCKS if s not in ok)
    p = parse_and_validate(rebal({"AAPL": 0.2, out: 0}), STOCKS, eligible=ok)
    assert dict(p.targets) == {"AAPL": 0.2, out: 0.0}
    with pytest.raises(OutputError, match=rf"{out} is not in input.eligible today \(AAPL, NVDA\)"):
        parse_and_validate(rebal({"AAPL": 0.2, out: 0.05}), STOCKS, eligible=ok)
    with pytest.raises(OutputError, match="SPY is context only"):
        parse_and_validate(rebal({"SPY": 0.0}), STOCKS, eligible=ok)
    parse_and_validate(rebal({out: 0.05}), STOCKS)               # other agents: no list


def agent7(script):
    client = FakeClient(script)
    return AIAgent(7, model_for(7), load_template(7), client, sleep=lambda s: None), client


def test_outside_target_is_retried_with_the_error_then_held():
    snap = make_snapshot(HD)
    out = next(s for s in stock_fields(snap) if s not in ret63_top(snap))
    bad = rebal({out: 0.05})
    agent, client = agent7([resp(bad), resp(bad)])
    dec = agent(snap, HOLDING, context())
    assert dec.weights is None and dec.record.status == "invalid" and len(client.calls) == 2
    assert f"{out} is not in input.eligible today" in client.prompts[1]
    good = ret63_top(snap)[0]
    agent, client = agent7([resp(bad), resp(rebal({good: 0.2, out: 0}))])
    dec = agent(snap, HOLDING, context())
    assert dec.record.status == "ok" and dict(dec.weights) == {good: 0.2, out: 0.0}


# ---------------------------------------------------------------------------
# apply_eligibility (execution/risk.py)
# ---------------------------------------------------------------------------

ELIG = ["AAPL", "NVDA", "LLY", "JPM", "META"]
CUR = {"AAPL": 0.18, "XOM": 0.21, "CVX": 0.0}


@pytest.mark.parametrize("kind", ["hold", "invalid", "unavailable", "capped"])
def test_a_hold_or_failed_day_sells_only_the_stock_off_the_list(kind):
    w, ev = apply_eligibility(None, CUR, ELIG, kind)
    assert w == {"AAPL": 0.18, "XOM": 0.0}                      # AAPL keeps its weight: no trade
    assert len(ev) == 1 and ev[0]["event_type"] == "forced_reduction" and ev[0]["rule"] == "eligibility"
    assert (ev[0]["symbol"], ev[0]["requested_weight"], ev[0]["applied_weight"]) == ("XOM", 0.21, 0.0)
    d = json.loads(ev[0]["detail"])
    assert d == {"decision": kind, "eligible": ELIG, "current_weight": 0.21}


def test_a_rebalance_logs_the_sale_of_a_dropped_stock():
    w, ev = apply_eligibility({"AAPL": 0.2, "NVDA": 0.2}, CUR, ELIG, "rebalance")
    assert w == {"AAPL": 0.2, "NVDA": 0.2, "XOM": 0.0}
    assert [(e["symbol"], e["requested_weight"]) for e in ev] == [("XOM", 0.0)]
    w, ev = apply_eligibility({"AAPL": 0.2, "XOM": 0.0}, CUR, ELIG, "rebalance")   # the model sold it itself
    assert w == {"AAPL": 0.2, "XOM": 0.0} and [e["symbol"] for e in ev] == ["XOM"]


def test_nothing_off_the_list_changes_nothing():
    assert apply_eligibility(None, {"AAPL": 0.2}, ELIG, "hold") == (None, [])
    assert apply_eligibility({"NVDA": 0.1}, {"AAPL": 0.2}, ELIG, "rebalance") == ({"NVDA": 0.1}, [])
    assert apply_eligibility(None, {}, ELIG, "hold") == (None, [])
    with pytest.raises(ValueError):
        apply_eligibility(None, {}, ELIG, "maybe")


# ---------------------------------------------------------------------------
# The runner: the sale is placed on hold, invalid and rebalance days, and on a restart
# ---------------------------------------------------------------------------

class Scripted(TR.Client):
    """Agent 7: day 1 buys the top 2 eligible; later days reply with `later(eligible, held)`."""

    def __init__(self, later):
        super().__init__("7")
        self.later = later

    def complete(self, prompt, *, model):
        self.calls.append(prompt)
        inp = json.loads(prompt.split("\nINPUT\n", 1)[1])
        if len(self.calls) == 1:
            return TR.reply(model, {s: 0.2 for s in inp["eligible"][:2]})
        return TR.reply(model, None, text=self.later(inp["eligible"], sorted(inp["portfolio"]["positions"])))


LATER = {
    "hold": lambda elig, held: '{"action": "hold", "reasoning": "keep"}',
    "invalid": lambda elig, held: "not json",
    "rebalance": lambda elig, held: rebal({held[0]: 0, held[1]: 0.2}),
}


def drop_first_held(monkeypatch, held):
    """From day 2, the first held stock is no longer in the list (both the prompt's and the runner's)."""
    real = ret63_top

    def fake(snapshot, n=5):
        top = real(snapshot, n)
        return top if snapshot.decision_date == DAYS[0].isoformat() else [s for s in top if s != held[0]]
    monkeypatch.setattr(harness, "ret63_top", fake)
    monkeypatch.setattr(daily, "ret63_top", fake)


@pytest.mark.parametrize("kind", ["hold", "invalid", "rebalance"])
def test_runner_sells_a_stock_that_left_the_list(conn, kind, monkeypatch):      # noqa: F811
    clients = {aid: TR.Client(aid) for aid in roster.EQUITY_AI + roster.CRYPTO_AI}
    clients["7"] = Scripted(LATER[kind])
    equity_run(conn, DAYS[0], clients)
    held = sorted(r[0] for r in conn.execute("SELECT symbol FROM orders WHERE agent_id = '7'"))
    assert len(held) == 2
    drop_first_held(monkeypatch, held)
    r, _ = equity_run(conn, DAYS[1], clients)
    assert r.agents["7"].decision == ("ok" if kind != "invalid" else "invalid"), r.agents["7"]
    ev = conn.execute("SELECT * FROM risk_events WHERE agent_id = '7' AND rule = 'eligibility'").fetchall()
    assert [(e["symbol"], e["event_type"], e["applied_weight"]) for e in ev] == [(held[0], "forced_reduction", 0.0)]
    assert json.loads(ev[0]["detail"])["decision"] == kind
    assert ev[0]["decision_id"] is not None
    day2 = conn.execute("SELECT symbol, side, qty, origin FROM orders WHERE agent_id = '7' AND created_at >= ?",
                        (TR.db.utc_ts(TR.close_of(DAYS[1])),)).fetchall()
    sells = [(o["symbol"], o["side"], o["origin"]) for o in day2 if o["symbol"] == held[0]]
    assert sells == [(held[0], "sell", "decision")]
    pos = conn.execute("SELECT qty FROM positions WHERE agent_id = '7' AND symbol = ? ORDER BY position_id DESC "
                       "LIMIT 1", (held[0],)).fetchone()
    assert day2[[o["symbol"] for o in day2].index(held[0])]["qty"] == pytest.approx(pos["qty"])  # all of it
    if kind != "rebalance":
        assert [o["symbol"] for o in day2] == [held[0]]          # a kept stock gets no trade


def test_restart_places_the_same_sale(conn, monkeypatch):      # noqa: F811
    clients = {aid: TR.Client(aid) for aid in roster.EQUITY_AI + roster.CRYPTO_AI}
    clients["7"] = Scripted(LATER["hold"])
    equity_run(conn, DAYS[0], clients)
    held = sorted(r[0] for r in conn.execute("SELECT symbol FROM orders WHERE agent_id = '7'"))
    drop_first_held(monkeypatch, held)
    real = TR.LD.place_orders

    def flaky(conn_, agent_id, *a, **kw):
        if agent_id == "7":
            raise RuntimeError("disk full")
        return real(conn_, agent_id, *a, **kw)
    monkeypatch.setattr(TR.LD, "place_orders", flaky)
    r, _ = equity_run(conn, DAYS[1], clients)
    assert r.exit_code == 1
    monkeypatch.setattr(TR.LD, "place_orders", real)
    calls = len(clients["7"].calls)
    r2, _ = equity_run(conn, DAYS[1], clients, at=cal.equity_times(DAYS[1], conn).decide_at + timedelta(minutes=5))
    assert r2.exit_code == 0, r2.errors
    assert r2.agents["7"].decision.startswith("stored") and len(clients["7"].calls) == calls
    day2 = conn.execute("SELECT symbol, side FROM orders WHERE agent_id = '7' AND created_at >= ?",
                        (TR.db.utc_ts(TR.close_of(DAYS[1])),)).fetchall()
    assert [(o["symbol"], o["side"]) for o in day2] == [(held[0], "sell")]
    assert conn.execute("SELECT COUNT(*) FROM risk_events WHERE agent_id = '7' AND rule = 'eligibility'"
                        ).fetchone()[0] == 1                      # logged once, not again on the restart


def test_profile_and_cash_floor_unchanged():
    from execution.ledger import has_cash_floor
    from execution.risk import profile
    p = profile(7)
    assert (p.max_per_stock, p.shorts, p.max_gross, p.max_total_short) == (0.20, False, 1.0, 0.0)
    assert has_cash_floor(7) and "7" in roster.EQUITY_AI


# ---------------------------------------------------------------------------
# Agents 14 and 15 (roster): Claude like agent 8, Low and High limits, their own templates
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("aid,limits", [
    ("14", {"max_per_stock": 0.10, "shorts_allowed": False, "max_gross": 1.0, "max_total_short": 0.0}),
    ("15", {"max_per_stock": 0.30, "shorts_allowed": True, "max_gross": 1.5, "max_total_short": 0.50}),
])
def test_agents_14_and_15_in_the_roster(aid, limits):
    assert aid in roster.EQUITY_AI and aid not in roster.EQUITY_NOT_BUILT
    agent = roster.ai_agent(aid, FakeClient([]), sleep=lambda s: None)
    assert agent.model == model_for(8) == model_for(aid)
    assert agent.template == load_template(aid) != load_template(8)
    p = harness.build_prompt(agent.template, aid, make_snapshot(HD), HOLDING, context())
    inp = json.loads(p.split("\nINPUT\n")[1])
    assert inp["limits"] == limits and "eligible" not in inp
    assert roster.EQUITY_NOT_BUILT == ("13",)
