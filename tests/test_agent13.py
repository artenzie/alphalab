"""Agent 13, the Adaptive blender (§5, v1.16): output spec, validation, inputs and the book."""

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agents import harness  # noqa: E402
from agents.harness import AIAgent, OutputError, blend, build_prompt, parse_allocation  # noqa: E402
from agents.llm import FakeClient  # noqa: E402
from agents.models import model_for  # noqa: E402
from agents.templates import load as load_template  # noqa: E402
from strategies.base import PastDecision  # noqa: E402
from test_harness import HOLDING, MEMORY, context, resp  # noqa: E402
from test_harness import D  # noqa: E402
from test_strategies import RUN, make_snapshot  # noqa: E402

README = ROOT / "agents" / "prompts" / "README.md"
STATS = {"ret_5": None, "ret_21": None, "return_since_start": 0.0, "vol_20": None, "max_drawdown": None}
STRATEGIES = {
    "3": {"weights": {"AAPL": 0.2, "NVDA": 0.2}, **STATS},
    "4": {"weights": {"XOM": -0.1, "AAPL": 0.1}, **STATS},
    "5": {"weights": {}, **STATS},
    "6": {"weights": {"AAPL": 1 / 6, "TSLA": -1 / 6}, **STATS},
}
ALLOC = {"3": 0.4, "4": 0.2, "5": 0.1, "6": 0.3}
MEM13 = replace(MEMORY, strategies=STRATEGIES, allocation=None,
                last_decisions=(PastDecision(RUN[3], "ok", {"AAPL": 0.1}, {}, "first", ALLOC),
                                PastDecision(RUN[4], "ok", None, {}, "kept")))


def allocate(alloc=ALLOC, **over):
    obj = {"action": "allocate", "allocation": alloc, "reasoning": "blend", "confidence": 0.6, **over}
    return json.dumps(obj)


def test_spec_is_the_readme_block_byte_for_byte():
    block = README.read_text(encoding="utf-8").split("## OUTPUT_SPEC_BLENDER (agent 13)\n\n~~~\n")[1]
    assert harness.OUTPUT_SPEC_BLENDER == block.split("\n~~~")[0]


def test_prompt_assembly_and_inputs():
    snap = make_snapshot(D)
    p = build_prompt(load_template(13), 13, snap, HOLDING, context(memory=MEM13))
    head, body = p.split("\n\nINPUT\n")
    assert head == load_template(13) + "\n\n" + harness.OUTPUT_SPEC_BLENDER
    inp = json.loads(body)
    assert set(inp) == {"date", "snapshot", "portfolio", "limits", "last_decisions", "run", "strategies",
                        "allocation"}
    assert inp["strategies"] == json.loads(json.dumps(STRATEGIES)) and inp["allocation"] is None
    assert inp["limits"] == {"max_per_stock": 0.2, "shorts_allowed": True, "max_gross": 1.0,
                             "max_total_short": 0.5}
    assert inp["last_decisions"] == [
        {"date": RUN[3].isoformat(), "status": "ok", "action": "allocate", "allocation": ALLOC, "reasoning": "first"},
        {"date": RUN[4].isoformat(), "status": "ok", "action": "hold", "allocation": None, "reasoning": "kept"}]
    with_alloc = build_prompt(load_template(13), 13, snap, HOLDING, context(memory=replace(MEM13, allocation=ALLOC)))
    assert json.loads(with_alloc.split("\nINPUT\n")[1])["allocation"] == ALLOC
    p8 = json.loads(build_prompt(load_template(8), 8, snap, HOLDING, context(memory=MEM13)).split("\nINPUT\n")[1])
    assert "strategies" not in p8 and "allocation" not in p8       # nobody else sees other agents
    with pytest.raises(ValueError, match="input.strategies"):
        build_prompt(load_template(13), 13, snap, HOLDING, context())


def test_valid_replies():
    p = parse_allocation(allocate())
    assert dict(p.allocation) == ALLOC and p.targets is None and p.confidence == 0.6
    h = parse_allocation('{"action": "hold", "reasoning": "keep"}')
    assert h.allocation is None and h.targets is None
    edge = {"3": 0.7, "4": 0.3, "5": 0, "6": 0.0}
    assert dict(parse_allocation(allocate(edge)).allocation) == {"3": 0.7, "4": 0.3, "5": 0.0, "6": 0.0}
    near = {"3": 0.25, "4": 0.25, "5": 0.25, "6": 0.25 + 9e-7}      # within 1e-6 of 1
    parse_allocation(allocate(near))
    parse_allocation("```json\n" + allocate() + "\n```")


@pytest.mark.parametrize("alloc,fragment", [
    ({"3": 0.4, "4": 0.3, "5": 0.3}, "exactly"),
    ({"3": 0.4, "4": 0.2, "5": 0.1, "6": 0.2, "7": 0.1}, "exactly"),
    ({"3": 0.71, "4": 0.29, "5": 0, "6": 0}, "from 0 to 0.7"),
    ({"3": -0.1, "4": 0.5, "5": 0.3, "6": 0.3}, "from 0 to 0.7"),
    ({"3": 0.4, "4": 0.2, "5": 0.1, "6": 0.31}, "add up to 1"),
    ({"3": 0.25, "4": 0.25, "5": 0.25, "6": 0.25 + 2e-6}, "add up to 1"),
    ({"3": "0.4", "4": 0.2, "5": 0.1, "6": 0.3}, "finite number"),
    ({"3": True, "4": 0.2, "5": 0.1, "6": 0.7}, "finite number"),
])
def test_invalid_allocations(alloc, fragment):
    with pytest.raises(OutputError, match=fragment):
        parse_allocation(allocate(alloc))


@pytest.mark.parametrize("text,fragment", [
    (allocate(confidence=None), "confidence is required"),
    (allocate(confidence=1.5), "confidence"),
    (allocate(reasoning=""), "reasoning"),
    ('{"action": "rebalance", "targets": {}, "reasoning": "x", "confidence": 0.5}', "'hold' or 'allocate'"),
    ('{"action": "hold", "reasoning": "x", "allocation": {}}', "a hold has no allocation"),
    ('{"action": "allocate", "allocation": [0.25, 0.25, 0.25, 0.25], "reasoning": "x", "confidence": 0.5}',
     "allocation must be an object"),
    ('{"action": "allocate", "allocation": {"3": NaN, "4": 0.2, "5": 0.1, "6": 0.3}, "reasoning": "x", '
     '"confidence": 0.5}', "not a valid number"),
])
def test_invalid_replies(text, fragment):
    with pytest.raises(OutputError, match=fragment):
        parse_allocation(text)


def test_blend():
    book = blend(ALLOC, STRATEGIES)
    assert book == pytest.approx({"AAPL": 0.4 * 0.2 + 0.2 * 0.1 + 0.3 / 6, "NVDA": 0.08, "XOM": -0.02,
                                  "TSLA": -0.05})
    assert blend({"3": 0, "4": 0, "5": 1.0, "6": 0}, STRATEGIES) == {}       # all in a cash strategy


def agent13(script):
    client = FakeClient(script)
    return AIAgent(13, model_for(13), load_template(13), client, sleep=lambda s: None), client


def test_decide_returns_the_book_and_stores_the_allocation():
    agent, client = agent13([resp(allocate())])
    dec = agent(make_snapshot(D), HOLDING, context(memory=MEM13))
    assert dec.record.status == "ok" and dict(dec.weights) == blend(ALLOC, STRATEGIES)
    row = dec.record.attempts[0].row()
    assert json.loads(row["parsed_allocation"]) == ALLOC
    assert json.loads(row["parsed_targets"]) == pytest.approx(blend(ALLOC, STRATEGIES))
    assert model_for(13) == model_for(8)


def test_invalid_is_retried_with_the_error_then_held():
    bad = allocate({"3": 0.8, "4": 0.2, "5": 0, "6": 0})
    agent, client = agent13([resp(bad), resp(bad)])
    dec = agent(make_snapshot(D), HOLDING, context(memory=MEM13))
    assert dec.weights is None and dec.record.status == "invalid" and len(client.calls) == 2
    assert "weight for 3 must be from 0 to 0.7" in client.prompts[1]
    agent, client = agent13([resp(bad), resp('{"action": "hold", "reasoning": "keep"}')])
    dec = agent(make_snapshot(D), HOLDING, context(memory=MEM13))
    assert dec.weights is None and dec.record.status == "ok"
    assert dec.record.attempts[-1].row()["parsed_allocation"] is None
    assert dec.record.attempts[-1].row()["parsed_targets"] == "null"


def test_other_agents_rows_have_no_allocation():
    from test_harness import REBAL
    agent = AIAgent(8, model_for(8), load_template(8), FakeClient([resp(REBAL)]), sleep=lambda s: None)
    row = agent(make_snapshot(D), HOLDING, context()).record.attempts[0].row()
    assert row["parsed_allocation"] is None
