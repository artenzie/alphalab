"""Prompt templates (agents/templates.py, agents/prompts/README.md; protocol §8 v1.15)."""

import hashlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agents import harness, templates  # noqa: E402
from runner import experiment, roster  # noqa: E402

PROMPTS = ROOT / "agents" / "prompts"
EQUITY = ("7", "8", "9", "10", "11", "12", "14", "15")


def read(name):
    return (PROMPTS / name).read_text(encoding="utf-8").rstrip()


def test_assembled_as_the_readme_says():
    common = read("equity_common.md")
    goals = {"7": "goal_constrained.md", "8": "goal_autonomous.md", "14": "goal_cautious.md",
             "15": "goal_aggressive.md"}
    for a, g in goals.items():
        assert templates.load(a) == common + "\n\n" + read(g)
    assert templates.load("C1") == read("crypto_c1.md")
    assert set(templates.TEMPLATE_FILES) == {*EQUITY, "C1"}          # 13 is wired in separately


def test_autonomous_byte_identical_and_others_differ():
    t8 = templates.load("8").encode("utf-8")
    for a in ("9", "10", "11", "12"):
        assert templates.load(a).encode("utf-8") == t8
    assert len({templates.load(a) for a in ("7", "8", "14", "15", "C1")}) == 5


def test_no_trailing_whitespace_and_utf8():
    for a in templates.TEMPLATE_FILES:
        t = templates.load(a)
        assert t == t.rstrip() and t.strip()
        t.encode("utf-8")


@pytest.mark.parametrize("agent_id", sorted(templates.TEMPLATE_FILES))
def test_no_date_or_experiment_name(agent_id):
    t = templates.load(agent_id)
    assert not re.search(r"\d{4}-\d{2}-\d{2}", t)
    for name in {experiment.OFFICIAL, experiment.SHAKEDOWN, "dev"}:
        assert not re.search(rf"\b{re.escape(name)}\b", t, re.IGNORECASE), name


def test_hashes_available():
    hashes = templates.template_hashes()
    assert set(hashes) == set(templates.TEMPLATE_FILES)
    for a, h in hashes.items():
        assert h == hashlib.sha256(templates.load(a).encode("utf-8")).hexdigest() == harness.sha256(templates.load(a))
    assert len({hashes[a] for a in ("8", "9", "10", "11", "12")}) == 1
    assert len(set(hashes.values())) == 5


def test_roster_uses_the_real_templates():
    for a in templates.TEMPLATE_FILES:
        assert roster.TEMPLATES[a] == templates.load(a)
        assert "PLACEHOLDER" not in roster.TEMPLATES[a]
    assert not hasattr(harness, "PLACEHOLDER_TEMPLATE") and not hasattr(harness, "CRYPTO_PLACEHOLDER_TEMPLATE")


def test_unknown_agent():
    with pytest.raises(KeyError):
        templates.load("13")


def test_cli_prints_every_hash(capsys):
    assert templates.main() == 0
    out = capsys.readouterr().out
    for h in templates.template_hashes().values():
        assert h in out
