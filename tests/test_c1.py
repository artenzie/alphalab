"""C1 · AI crypto trader (protocol §13, §8, v1.12): AIAgent on a crypto snapshot, §13 limits by the risk checks.

FakeClient only: no network. Ledger end-to-end uses the helpers in test_ledger_crypto.
"""

import json
import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agents import harness  # noqa: E402
from agents.harness import AIAgent, build_prompt  # noqa: E402
from agents.templates import load as load_template  # noqa: E402

CRYPTO_TEMPLATE = load_template("C1")
from agents.llm import FakeClient, LLMResponse  # noqa: E402
from agents.models import CLAUDE, model_for  # noqa: E402
from data.snapshot import Bar, Snapshot, canonical_json, compute_fields, sha256  # noqa: E402
from execution import ledger_db as LD  # noqa: E402
from execution.risk import apply_risk  # noqa: E402
from strategies.base import AgentMemory, Context, Portfolio  # noqa: E402
from test_ledger_crypto import BTC, MON, TUE, add, conn, decide_at, fill_bar, place, settle  # noqa: E402,F401
from test_strategies import make_snapshot  # noqa: E402

COINS = ("BTC/USD", "DOGE/USD", "ETH/USD", "SOL/USD", "XRP/USD")
PRICE = {"BTC/USD": 65000.0, "DOGE/USD": 0.06, "ETH/USD": 1800.0, "SOL/USD": 72.0, "XRP/USD": 0.97}
D = date(2026, 11, 16)
EMPTY = Portfolio(cash=100_000.0, value=100_000.0)


def crypto_snapshot(d=D, n=120):
    bars = {s: tuple(Bar((d - timedelta(days=n - 1 - i)).isoformat(), PRICE[s], PRICE[s] * 1.01, PRICE[s] * 0.99,
                         PRICE[s] * (1 + 0.001 * (i - n + 1)) if i < n - 1 else PRICE[s], 1e6)
                     for i in range(n)) for s in COINS}
    hist = sha256(canonical_json({s: [list(b) for b in bars[s]] for s in bars}))
    payload = {"version": 1, "asset_class": "crypto", "decision_date": d.isoformat(), "mode": "backtest",
               "cutoff_ts": f"{(d + timedelta(days=1)).isoformat()}T00:10:00.000Z", "history_sha256": hist,
               "symbols": {s: compute_fields(list(bars[s]), "crypto") for s in bars}}
    text = canonical_json(payload)
    return Snapshot("crypto", d.isoformat(), "backtest", payload["cutoff_ts"], text, sha256(text), hist, bars)


def ctx(d=D):
    return replace(Context(d, False, False, False), memory=AgentMemory(starting_value=100_000.0))


def resp(text):
    return LLMResponse(text, CLAUDE.model_id, "Anthropic", tokens_in=900, tokens_out=120, cost_usd=0.003,
                       latency_ms=700, reasoning_tokens=0, finish_reason="stop")


def rebalance(targets):
    return json.dumps({"action": "rebalance", "targets": targets, "reasons": {s: "why" for s in targets},
                       "reasoning": "Allocate.", "confidence": 0.6})


def c1(script):
    client = FakeClient([resp(t) for t in script])
    return AIAgent("C1", model_for("C1"), CRYPTO_TEMPLATE, client, sleep=lambda s: None), client


def test_c1_uses_agent_8s_claude_config():
    assert model_for("C1") is model_for(8) is CLAUDE


def test_crypto_prompt_has_the_coins_the_crypto_spec_and_c1_limits():
    p = build_prompt(CRYPTO_TEMPLATE, "C1", crypto_snapshot(), EMPTY, ctx())
    assert p.startswith(CRYPTO_TEMPLATE) and harness.OUTPUT_SPEC_CRYPTO in p
    assert harness.OUTPUT_SPEC not in p and "22 stocks" not in p and "SPY" not in p
    inp = json.loads(p.split("\nINPUT\n", 1)[1])
    assert sorted(inp["snapshot"]) == list(COINS)
    assert inp["limits"] == {"max_per_coin": 0.5, "shorts_allowed": False, "max_gross": 1.0,
                             "max_total_short": 0.0}
    assert inp["date"] == D.isoformat()


def test_asset_classes_cannot_be_mixed():
    with pytest.raises(ValueError, match="crypto snapshots"):
        build_prompt(CRYPTO_TEMPLATE, "C1", make_snapshot(D), EMPTY, ctx())
    with pytest.raises(ValueError, match="us_equity snapshots"):
        build_prompt(load_template(8), 8, crypto_snapshot(), EMPTY, ctx())


def test_valid_hold_and_rebalance():
    agent, _ = c1(['{"action": "hold", "reasoning": "wait"}'])
    assert agent(crypto_snapshot(), EMPTY, ctx()).is_hold
    agent, _ = c1([rebalance({"BTC/USD": 0.4, "ETH/USD": 0.3})])
    dec = agent(crypto_snapshot(), EMPTY, ctx())
    assert dict(dec.weights) == {"BTC/USD": 0.4, "ETH/USD": 0.3} and dec.record.status == "ok"


@pytest.mark.parametrize("bad", ["AAPL", "SPY", "BTC", "BNB/USD"])
def test_a_non_coin_target_is_invalid_and_retried_with_the_coin_message(bad):
    agent, client = c1([rebalance({bad: 0.2}), rebalance({"BTC/USD": 0.2})])
    dec = agent(crypto_snapshot(), EMPTY, ctx())
    a1, a2 = dec.record.attempts
    assert a1.status == "invalid" and "use only the 5 coins" in a1.error and a2.status == "ok"
    assert "YOUR PREVIOUS REPLY WAS INVALID" in client.prompts[1]


def test_the_50_percent_cap_and_long_only_are_enforced_by_the_risk_checks():
    """The harness passes over-limit weights through; apply_risk('C1') clips them (§13, not just the prompt)."""
    agent, _ = c1([rebalance({"BTC/USD": 0.8, "ETH/USD": 0.6, "SOL/USD": 0.3, "XRP/USD": -0.2})])
    dec = agent(crypto_snapshot(), EMPTY, ctx())
    assert dict(dec.weights)["BTC/USD"] == 0.8                    # harness: no limit checks
    w, events = apply_risk("C1", dec, {}, {})
    rules = [(e["rule"], e["symbol"]) for e in events]
    assert ("shorts_not_allowed", "XRP/USD") in rules
    assert ("max_per_stock", "BTC/USD") in rules and ("max_per_stock", "ETH/USD") in rules
    assert ("max_gross", "SOL/USD") in rules                     # 0.5 + 0.5 + 0.3 = 1.3 -> 1.0
    assert w["XRP/USD"] == 0.0 and max(w.values()) <= 0.5 + 1e-12
    assert sum(w.values()) == pytest.approx(1.0)
    assert w == pytest.approx({"BTC/USD": 0.5 / 1.3, "ETH/USD": 0.5 / 1.3, "SOL/USD": 0.3 / 1.3, "XRP/USD": 0.0})


def test_c1_over_the_cap_through_risk_orders_and_fills(conn):
    """80% BTC asked -> 50% at the decision close -> filled at the minute bar, never above 50%."""
    add(conn, BTC, MON, 65000.0)
    settle(conn, "C1", MON)
    agent, _ = c1([rebalance({"BTC/USD": 0.8})])
    dec = agent(crypto_snapshot(MON), EMPTY, ctx(MON))
    w, events = apply_risk("C1", dec, {}, {})
    assert w == {"BTC/USD": 0.5} and events[0]["rule"] == "max_per_stock"
    placed = place(conn, "C1", MON, {BTC: 65000.0}, w)
    fill_bar(conn, BTC, MON, 0, 65000.0)
    add(conn, BTC, TUE, 65000.0)
    res = settle(conn, "C1", TUE)
    assert res.fills[0].qty == placed.orders[0].qty and not res.floor_events
    acct = LD.load_account(conn, "C1", as_of=TUE)
    assert acct.shares(BTC) * 65000.0 / 100_000.0 <= 0.5          # sized at the decision close and value
    # After the fill the fee and slippage (~0.3% of the trade) leave the portfolio a little smaller,
    # so the same coins weigh a hair over 50% at an unchanged price: the cap is a decision-time limit.
    assert acct.shares(BTC) * 65000.0 / acct.value({BTC: 65000.0}) < 0.5 * (1 + 0.5 * 0.0031)
