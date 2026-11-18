"""Which agents the daily runner runs (protocol §5, §13), and how each is built.

The one place that maps an agent id to its decide function or AI config. The
real prompts (Sat 5 Dec, agents/prompts/) replace the placeholder templates in
TEMPLATES; rule 3 freezes this file from Mon 11 Jan 2027.
"""

from __future__ import annotations

from typing import Callable, Sequence

from agents.harness import (BACKOFF_S, CRYPTO_PLACEHOLDER_TEMPLATE, PLACEHOLDER_TEMPLATE, AIAgent)
from agents.llm import LLMClient
from agents.models import model_for
from strategies import registry
from strategies.base import Agent

EQUITY_RULE = ("1", "2", "3", "4", "5", "6")
EQUITY_AI = ("8", "9", "10", "11", "12")
EQUITY_NOT_BUILT = ("7", "13", "14", "15")      # skipped, with a log line, until built
CRYPTO_RULE = ("C0",)
CRYPTO_AI = ("C1",)
CRYPTO_NOT_BUILT: tuple[str, ...] = ()

TEMPLATES = {
    "8": PLACEHOLDER_TEMPLATE, "9": PLACEHOLDER_TEMPLATE, "10": PLACEHOLDER_TEMPLATE,
    "11": PLACEHOLDER_TEMPLATE, "12": PLACEHOLDER_TEMPLATE,
    "C1": CRYPTO_PLACEHOLDER_TEMPLATE,
}


def rule_ids(asset_class: str) -> tuple[str, ...]:
    return EQUITY_RULE if asset_class == "us_equity" else CRYPTO_RULE


def ai_ids(asset_class: str) -> tuple[str, ...]:
    return EQUITY_AI if asset_class == "us_equity" else CRYPTO_AI


def not_built(asset_class: str) -> tuple[str, ...]:
    return EQUITY_NOT_BUILT if asset_class == "us_equity" else CRYPTO_NOT_BUILT


def rule_agent(agent_id: str) -> Agent:
    return registry.get_agent(int(agent_id) if agent_id.isdigit() else agent_id)


def ai_agent(agent_id: str, client: LLMClient, sleep: Callable[[float], None],
             backoff: Sequence[float] = BACKOFF_S) -> AIAgent:
    return AIAgent(agent_id, model_for(agent_id), TEMPLATES[agent_id], client, sleep=sleep, backoff=backoff)
