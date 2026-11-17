"""The frozen lineup (agents/models.py) matches protocol §8 (v1.10)."""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents.models import AGENT_MODELS, CLAUDE, MAX_TOKENS, MODELS, model_for  # noqa: E402
from execution.risk import profile  # noqa: E402

PROTOCOL = (ROOT / "research" / "experiment_protocol.md").read_text()
SECTION8 = PROTOCOL.split("## 8 ·")[1].split("## 9 ·")[0]


def row(name: str) -> str:
    m = re.search(rf"^\| {re.escape(name)} \| (.+) \|$", SECTION8, re.M)
    assert m, f"§8 has no row {name!r}"
    return m.group(1)


def test_model_ids_and_makers_match_section_8():
    pairs = re.findall(r"`([^`]+)` \(([^)]+)\)", row("Model IDs"))
    assert [p[0] for p in pairs] == [m.model_id for m in MODELS.values()]
    makers = [p[1] for p in pairs]
    for maker, m in zip(makers, MODELS.values()):
        assert any(n == maker or n.startswith(maker) for n in m.provider_names), (maker, m.provider_names)


def test_endpoints_match_section_8():
    listed = set(re.findall(r"`([a-z-]+)`", row("Access")))
    assert listed == {e for m in MODELS.values() for e in m.endpoints}


def test_reasoning_medium_for_all_five():
    assert "**medium**" in row("Reasoning") and "all five" in row("Reasoning")
    assert {m.reasoning for m in MODELS.values()} == {"medium"}


def test_max_output_for_all_five():
    assert "16,000 tokens" in row("Max output") and MAX_TOKENS == 16_000
    assert {m.max_tokens for m in MODELS.values()} == {16_000}


def test_temperature_matches_section_8():
    text = row("Temperature")
    zero = re.search(r"allows it \(([^)]+)\)", text).group(1)
    none = re.search(r"provider default applies \(([^)]+)\)", text).group(1)
    assert {m.name for m in MODELS.values() if m.temperature == 0.0} == {n.strip().lower() for n in zero.split(",")}
    assert {m.name for m in MODELS.values() if m.temperature is None} == \
        {n.strip().lower() for n in none.split(",")}


def test_claude_agents_share_agent_8s_model():
    assert row("Agents 7, 13, 14, 15 and C1") == "Same Claude model version as agent 8"
    for a in ("7", "13", "14", "15", "C1"):
        assert model_for(a) is model_for(8) is CLAUDE


def test_autonomous_agents_8_to_12_one_model_each():
    assert [model_for(a).name for a in range(8, 13)] == ["claude", "gpt", "gemini", "deepseek", "grok"]


def test_every_ai_agent_has_a_risk_profile():
    for a in AGENT_MODELS:
        profile(a)


@pytest.mark.parametrize("agent", [1, 6, "C0", 16])
def test_non_ai_agents_have_no_model(agent):
    with pytest.raises(KeyError):
        model_for(agent)


def test_test_pinned_uses_this_lineup():
    src = (ROOT / "scripts" / "test_pinned.py").read_text()
    assert "from agents.models import" in src and "LINEUP = [" not in src
