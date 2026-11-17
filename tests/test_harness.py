"""The AI agent harness (agents/harness.py, agents/llm.py): protocol §4, §5 (v1.9), §8 (v1.10).

FakeClient only: no network, no API keys. Snapshots come from the synthetic
make_snapshot in test_strategies.py.
"""

import ast
import json
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agents import harness  # noqa: E402
from agents.harness import (AIAgent, AIDecision, OutputError, PLACEHOLDER_TEMPLATE,  # noqa: E402
                            build_prompt, parse_and_validate, sha256)
from agents.llm import FakeClient, LLMCapped, LLMResponse, LLMUnavailable, model_matches  # noqa: E402
from agents.models import CLAUDE, GEMINI  # noqa: E402
from data import db  # noqa: E402
from strategies.base import AgentMemory, Decision, PastDecision, Portfolio  # noqa: E402
from test_strategies import EMPTY, RUN, STOCKS, ctx, make_snapshot, sealed  # noqa: E402

D = RUN[5]
MODEL, PROVIDER = CLAUDE.model_id, "Anthropic"
MEMORY = AgentMemory(
    starting_value=100_000.0, avg_cost={"AAPL": 210.0},
    last_decisions=tuple(PastDecision(RUN[i], "ok", {"AAPL": 0.1}, {"AAPL": "trend"}, f"day {i}")
                         for i in range(5)) + (PastDecision(RUN[4], "invalid", None, {}, "held"),))
HOLDING = Portfolio(cash=50_000.0, positions={"AAPL": 100.0, "NVDA": -10.0}, value=100_000.0)

HOLD = '{"action": "hold", "reasoning": "Nothing has changed."}'
REBAL = json.dumps({"action": "rebalance", "targets": {"AAPL": 0.2, "NVDA": -0.1},
                    "reasons": {"AAPL": "strong momentum", "NVDA": "overextended"},
                    "reasoning": "Rotate into AAPL.\nShort NVDA.", "confidence": 0.7})


def context(d=D, memory=MEMORY):
    return replace(ctx(d), memory=memory)


def resp(text, provider=PROVIDER, model=MODEL, finish="stop"):
    return LLMResponse(text, model, provider, tokens_in=1200, tokens_out=150, cost_usd=0.0042, latency_ms=850,
                       reasoning_tokens=40, finish_reason=finish)


def run(script, portfolio=HOLDING, d=D, agent_id=8, config=CLAUDE):
    client, sleeps = FakeClient(script), []
    agent = AIAgent(agent_id, config, PLACEHOLDER_TEMPLATE, client, sleep=sleeps.append)
    return agent(make_snapshot(d), portfolio, context(d)), client, sleeps


def invalid(text):
    with pytest.raises(OutputError) as e:
        parse_and_validate(text, STOCKS)
    return str(e.value)


# ---------------------------------------------------------------------------
# Valid output
# ---------------------------------------------------------------------------

def test_valid_hold():
    dec, client, _ = run([resp(HOLD)])
    assert isinstance(dec, AIDecision) and isinstance(dec, Decision)
    assert dec.is_hold and dec.reason == "Nothing has changed."
    rec = dec.record
    assert rec.status == "ok" and len(rec.attempts) == 1 and len(client.calls) == 1
    row = rec.attempts[0].row()
    assert row["parsed_targets"] == "null" and row["reasons"] is None and row["status"] == "ok"


def test_valid_rebalance():
    dec, client, _ = run([resp(REBAL)])
    assert dict(dec.weights) == {"AAPL": 0.2, "NVDA": -0.1}
    assert dec.reason == "Rotate into AAPL. Short NVDA."          # one line
    a = dec.record.attempts[0]
    assert dec.record.status == "ok" and a.status == "ok" and a.error is None
    row = a.row()
    assert json.loads(row["parsed_targets"]) == {"AAPL": 0.2, "NVDA": -0.1}
    assert json.loads(row["reasons"]) == {"AAPL": "strong momentum", "NVDA": "overextended"}
    assert row["overall_reasoning"] == "Rotate into AAPL.\nShort NVDA."
    assert row["confidence"] == 0.7
    assert (row["tokens_in"], row["tokens_out"], row["cost_usd"], row["latency_ms"]) == (1200, 150, 0.0042, 850)
    assert (row["model_id"], row["provider_served"], row["raw_response"]) == (MODEL, PROVIDER, REBAL)
    assert row["prompt_hash"] == sha256(client.prompts[0])
    assert (row["temperature"], row["reasoning_setting"]) == (None, "medium")     # Claude takes no temperature
    assert a.reasoning_tokens == 40 and "reasoning_tokens" not in row
    assert client.calls[0]["model"] is CLAUDE


def test_empty_targets_mean_all_cash():
    text = '{"action":"rebalance","targets":{},"reasons":{},"reasoning":"risk off","confidence":0.5}'
    dec, _, _ = run([resp(text)])
    assert not dec.is_hold and dict(dec.weights) == {}


@pytest.mark.parametrize("text", [
    "```json\n" + REBAL + "\n```",
    "Here is my decision:\n```json\n" + REBAL + "\n```\nThanks.",
    "```\n" + REBAL + "\n```",
])
def test_fenced_json(text):
    dec, _, _ = run([resp(text)])
    assert dec.record.status == "ok" and dict(dec.weights) == {"AAPL": 0.2, "NVDA": -0.1}


def test_two_fenced_blocks_are_invalid():
    assert "2 blocks" in invalid("```json\n" + HOLD + "\n```\n```json\n" + REBAL + "\n```")


# ---------------------------------------------------------------------------
# Invalid output
# ---------------------------------------------------------------------------

def rebal(**over):
    obj = json.loads(REBAL)
    obj.update(over)
    return json.dumps(obj)


def test_bad_symbol():
    assert "unknown symbol 'MSFT'" in invalid(rebal(targets={"MSFT": 0.1}, reasons={"MSFT": "x"}))


def test_spy_target_is_context_only():
    msg = invalid(rebal(targets={"SPY": 0.5}, reasons={"SPY": "market"}))
    assert "SPY is context only" in msg
    # ... while SPY is still in the snapshot the model sees
    dec, client, _ = run([resp(rebal(targets={"SPY": 0.5}, reasons={"SPY": "m"})), resp(HOLD)])
    snap = json.loads(client.prompts[0].split("\nINPUT\n", 1)[1])["snapshot"]
    assert "SPY" in snap and "SPY is context only" in client.prompts[1]
    assert dec.record.attempts[0].status == "invalid"


def test_missing_reason():
    assert "missing reason for NVDA" in invalid(rebal(reasons={"AAPL": "x"}))
    assert "missing reason for NVDA" in invalid(rebal(reasons={"AAPL": "x", "NVDA": "  "}))


@pytest.mark.parametrize("weight", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nan_or_infinite_weight(weight):
    text = REBAL.replace('"NVDA": -0.1', f'"NVDA": {weight}')
    assert weight in text
    msg = invalid(text)
    assert "not a valid number" in msg or "finite" in msg


@pytest.mark.parametrize("over, fragment", [
    ({"targets": {"AAPL": True}, "reasons": {"AAPL": "x"}}, "finite number"),
    ({"targets": {"AAPL": "0.1"}, "reasons": {"AAPL": "x"}}, "finite number"),
    ({"confidence": 1.2}, "confidence"),
    ({"confidence": -0.1}, "confidence"),
    ({"confidence": "high"}, "confidence"),
    ({"confidence": None}, "confidence is required"),
    ({"action": "buy"}, "action must be"),
    ({"reasoning": ""}, "reasoning"),
    ({"targets": [["AAPL", 0.1]]}, "targets must be an object"),
    ({"reasons": "because"}, "reasons must be an object"),
])
def test_other_invalid_fields(over, fragment):
    assert fragment in invalid(rebal(**over))


@pytest.mark.parametrize("text, fragment", [
    ('{"action":"hold","reasoning":"a","reasoning":"b"}', "duplicate key"),
    ('{"action":"hold","reasoning":"x","targets":{}}', "a hold has no targets"),
    ("[1, 2]", "JSON object"),
    ("{not json", "not valid JSON"),
    ("I think we should hold.", "one JSON object"),
    ("", "empty reply"),
])
def test_malformed_replies(text, fragment):
    assert fragment in invalid(text)


# ---------------------------------------------------------------------------
# Retries (§8)
# ---------------------------------------------------------------------------

def test_retry_then_valid():
    bad = rebal(targets={"MSFT": 0.1}, reasons={"MSFT": "x"})
    dec, client, sleeps = run([resp(bad), resp(REBAL)])
    assert dict(dec.weights) == {"AAPL": 0.2, "NVDA": -0.1} and dec.record.status == "ok"
    a1, a2 = dec.record.attempts
    assert (a1.attempt, a1.status, a2.attempt, a2.status) == (1, "invalid", 2, "ok")
    assert "unknown symbol 'MSFT'" in a1.error and a1.raw_response == bad and a1.row()["parsed_targets"] is None
    p1, p2 = client.prompts
    assert p2.startswith(p1) and "YOUR PREVIOUS REPLY WAS INVALID: unknown symbol 'MSFT'" in p2
    assert (a1.prompt_hash, a2.prompt_hash) == (sha256(p1), sha256(p2)) and sleeps == []


def test_retry_then_invalid_holds():
    dec, client, _ = run([resp("not json"), resp(rebal(confidence=2))])
    assert dec.is_hold and dec.record.status == "invalid"
    assert [a.status for a in dec.record.attempts] == ["invalid", "invalid"]
    assert len(client.calls) == 2                       # one retry only
    assert dec.reason.startswith("held (invalid): confidence")


def test_client_down_holds_unavailable():
    dec, client, sleeps = run([LLMUnavailable("503")] * 4)
    assert dec.is_hold and dec.record.status == "unavailable"
    assert len(client.calls) == 4 and sleeps == [300, 600, 900] and sum(sleeps) == 1800
    assert [a.status for a in dec.record.attempts] == ["unavailable"] * 4
    assert all(a.provider_served is None and a.error == "unavailable: 503" for a in dec.record.attempts)
    assert all(c["model"] is CLAUDE for c in client.calls)


def test_client_down_then_recovers():
    dec, client, sleeps = run([LLMUnavailable("timeout"), LLMUnavailable("timeout"), resp(REBAL)])
    assert dec.record.status == "ok" and sleeps == [300, 600]
    assert [a.attempt for a in dec.record.attempts] == [1, 2, 3]
    assert len(set(client.prompts)) == 1                # same prompt on a transport retry


def test_wrong_provider_is_invalid():
    dec, client, _ = run([resp(REBAL, provider="SomeHost"), resp(REBAL, provider="SomeHost")])
    assert dec.is_hold and dec.record.status == "invalid"
    a1, a2 = dec.record.attempts
    assert "served by 'SomeHost', pinned to 'Anthropic'" in a1.error and a1.provider_served == "SomeHost"
    assert a1.parsed is None
    assert client.prompts[0] == client.prompts[1]      # model did nothing wrong: original prompt


def test_wrong_provider_then_right_provider():
    dec, _, _ = run([resp(REBAL, provider="SomeHost"), resp(REBAL)])
    assert dec.record.status == "ok" and not dec.is_hold


def test_second_official_provider_name_is_accepted():
    dec, _, _ = run([resp(REBAL, provider="Google", model=GEMINI.model_id)], agent_id=10, config=GEMINI)
    assert dec.record.status == "ok"
    assert dec.record.attempts[0].row()["temperature"] == 0.0


@pytest.mark.parametrize("reported", [MODEL, MODEL + "-20261013", MODEL + "-2026-10-13", MODEL + "-1013",
                                      MODEL + "@20261013"])
def test_model_id_dated_variants_match(reported):
    assert model_matches(MODEL, reported)


@pytest.mark.parametrize("reported", [None, "", "anthropic/claude-opus-5.5", MODEL + "-thinking", MODEL + "-pro",
                                      MODEL + "20261013", MODEL + "-202610", "x" + MODEL, MODEL[:-1]])
def test_model_id_other_models_do_not_match(reported):
    assert not model_matches(MODEL, reported)


def test_wrong_model_is_invalid_and_logs_both_ids():
    dec, client, _ = run([resp(REBAL, model="anthropic/claude-haiku-5.5"), resp(REBAL, model="anthropic/claude-haiku-5.5")])
    assert dec.is_hold and dec.record.status == "invalid"
    a1 = dec.record.attempts[0]
    assert a1.status == "invalid" and a1.parsed is None
    assert a1.row()["model_id"] == "anthropic/claude-haiku-5.5"                          # reported id in the row
    assert a1.error == f"wrong model: requested {MODEL!r}, reported 'anthropic/claude-haiku-5.5'"
    assert client.prompts[0] == client.prompts[1]      # original prompt on retry


def test_wrong_model_then_dated_variant_ok():
    dec, _, _ = run([resp(REBAL, model="openai/gpt-6.1-sol"), resp(REBAL, model=MODEL + "-20261013")])
    assert dec.record.status == "ok" and dec.record.attempts[1].row()["model_id"] == MODEL + "-20261013"


def test_cut_off_reply_is_invalid_even_if_it_parses():
    dec, client, _ = run([resp(HOLD, finish="length"), resp(REBAL)])
    assert dec.record.status == "ok"
    a1 = dec.record.attempts[0]
    assert a1.status == "invalid" and "cut off at the 16000-token output limit" in a1.error
    assert "YOUR PREVIOUS REPLY WAS INVALID" in client.prompts[1]


def test_capped_holds_without_retry():
    dec, client, sleeps = run([LLMCapped("budget")])
    assert dec.is_hold and dec.record.status == "capped" and len(client.calls) == 1 and sleeps == []


def test_unexpected_client_errors_propagate():
    with pytest.raises(RuntimeError):
        run([RuntimeError("bug")])


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def test_prompt_hash_stable():
    a, ca, _ = run([resp(HOLD)])
    b, cb, _ = run([resp(HOLD)])
    assert ca.prompts == cb.prompts
    assert a.record.attempts[0].prompt_hash == b.record.attempts[0].prompt_hash == sha256(ca.prompts[0])
    c, _, _ = run([resp(HOLD)], portfolio=EMPTY)
    assert c.record.attempts[0].prompt_hash != a.record.attempts[0].prompt_hash


def test_prompt_contents():
    _, client, _ = run([resp(HOLD)])
    prompt = client.prompts[0]
    assert prompt.startswith(PLACEHOLDER_TEMPLATE) and harness.OUTPUT_SPEC in prompt
    assert "COMPLETE target portfolio" in prompt and "leave out of targets is sold" in prompt
    assert "SPY is market context only" in prompt
    head, body = prompt.split("\nINPUT\n", 1)
    assert "\n" not in body and ": " not in body       # compact JSON, one line
    inp = json.loads(body)
    assert set(inp) == {"date", "snapshot", "portfolio", "limits", "last_decisions"}
    assert inp["date"] == D.isoformat()
    assert inp["snapshot"] == make_snapshot(D).payload["symbols"]
    assert all(f["last_date"] <= D.isoformat() for f in inp["snapshot"].values())
    pos = inp["portfolio"]["positions"]
    close = inp["snapshot"]["AAPL"]["last_close"]
    assert pos["AAPL"] == {"shares": 100.0, "last_close": close, "weight": round(100 * close / 100_000, 6),
                           "avg_cost": 210.0, "unrealised_pnl": round(100 * (close - 210.0), 6)}
    assert pos["NVDA"]["avg_cost"] is None and pos["NVDA"]["unrealised_pnl"] is None
    assert inp["portfolio"]["return_since_start"] == 0.0
    assert inp["limits"] == {"max_per_stock": 0.2, "shorts_allowed": True, "max_gross": 1.0, "max_total_short": 0.3}
    assert [p["date"] for p in inp["last_decisions"]] == [RUN[i].isoformat() for i in (1, 2, 3, 4, 4)]
    assert inp["last_decisions"][-1] == {"date": RUN[4].isoformat(), "status": "invalid", "action": "hold",
                                         "targets": None, "reasons": {}, "reasoning": "held"}


def test_prompt_limits_follow_the_agent():
    lim = json.loads(build_prompt("t", 14, make_snapshot(D), HOLDING, context()).split("\nINPUT\n")[1])["limits"]
    assert lim == {"max_per_stock": 0.1, "shorts_allowed": False, "max_gross": 1.0, "max_total_short": 0.0}


def test_prompt_guards():
    snap = make_snapshot(D)
    with pytest.raises(ValueError, match="snapshot is for"):
        build_prompt("t", 8, snap, HOLDING, context(d=RUN[6]))
    with pytest.raises(ValueError, match="memory"):
        build_prompt("t", 8, snap, HOLDING, ctx(D))
    future = AgentMemory(100_000.0, {}, (PastDecision(D, "ok", None),))
    with pytest.raises(ValueError, match="before the decision date"):
        build_prompt("t", 8, snap, HOLDING, context(memory=future))


def test_unknown_agent_id_fails_at_construction():
    with pytest.raises(KeyError):
        AIAgent(99, CLAUDE, "t", FakeClient([]))


# ---------------------------------------------------------------------------
# Rows fit the decisions table
# ---------------------------------------------------------------------------

def test_rows_insert_into_decisions_table(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    snap = make_snapshot(D)
    sid = conn.execute("INSERT INTO snapshots (asset_class, snapshot_date, cutoff_ts, built_at, payload, sha256,"
                       " checks, checks_passed) VALUES ('us_equity', ?, ?, ?, ?, ?, '{}', 1)",
                       (snap.decision_date, snap.cutoff_ts, snap.cutoff_ts, snap.canonical, snap.sha256)).lastrowid
    cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
    decisions = [run([resp(HOLD)])[0], run([resp(REBAL)])[0], run([resp("x"), resp(REBAL)])[0],
                 run([resp("x"), resp("y")])[0], run([LLMUnavailable("e")] * 4)[0], run([LLMCapped("c")])[0]]
    for i, dec in enumerate(decisions):
        for a in dec.record.attempts:
            row = a.row() | {"agent_id": str(8 + i), "snapshot_id": sid,
                             "decided_at": f"{snap.decision_date}T23:00:00.000Z"}
            assert set(row) <= cols
            conn.execute(f"INSERT INTO decisions ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                         tuple(row.values()))
    n = conn.execute("SELECT count(*) FROM decisions").fetchone()[0]
    assert n == sum(len(d.record.attempts) for d in decisions) == 1 + 1 + 2 + 2 + 4 + 1
    conn.close()


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

AGENT_FILES = sorted((ROOT / "agents").glob("*.py"))
ALLOWED = {"__future__", "hashlib", "json", "math", "re", "time", "dataclasses", "types", "typing",
           "strategies.base", "execution.risk", "agents.llm", "agents.models", "data.snapshot"}
NETWORK_FILES = {"openrouter.py"}        # the one module that talks to the outside (tests/test_openrouter.py)


@pytest.mark.parametrize("path", [p for p in AGENT_FILES if p.name not in NETWORK_FILES], ids=lambda p: p.stem)
def test_agent_files_import_only_allowed_modules(path):
    tree = ast.parse(path.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported <= ALLOWED, imported - ALLOWED
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"open", "eval", "exec", "__import__", "globals", "compile"}


def test_data_layer_only_for_type_hints():
    src = (ROOT / "agents" / "harness.py").read_text()
    assert "if TYPE_CHECKING:" in src and src.count("from data.") == 1


def test_harness_runs_sealed(monkeypatch):
    """Same decisions and records with every outside channel blocked."""
    scripts = [[resp(HOLD)], [resp(REBAL)], [resp("x"), resp(REBAL)], [resp("x"), resp("y")],
               [LLMUnavailable("e")] * 4, [LLMCapped("c")], [resp(REBAL, provider="Other"), resp(REBAL)]]
    cases = [(s, p, d) for s in scripts for p in (EMPTY, HOLDING) for d in (D, RUN[-1])]
    expected = [run(*c)[0] for c in cases]
    sealed(monkeypatch)
    got = [run(*c)[0] for c in cases]
    monkeypatch.undo()
    assert got == expected and [g.record for g in got] == [e.record for e in expected]
    assert any(not g.is_hold for g in got) and {g.record.status for g in got} == {"ok", "invalid", "unavailable", "capped"}
