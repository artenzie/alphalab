"""Which agents the daily runner runs (protocol §5, §13), and how each is built.

The one place that maps an agent id to its decide function or AI config. The
AI prompt templates come from agents/prompts/ (agents/templates.py); rule 3
freezes this file from Mon 11 Jan 2027.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Callable, Sequence

from agents import templates
from agents.harness import BACKOFF_S, AIAgent
from agents.llm import LLMClient
from agents.models import model_for
from strategies import registry
from strategies.base import Agent

EQUITY_RULE = ("1", "2", "3", "4", "5", "6")
EQUITY_AI = ("7", "8", "9", "10", "11", "12", "14", "15")
EQUITY_NOT_BUILT = ("13",)                      # skipped, with a log line, until built
CRYPTO_RULE = ("C0",)
CRYPTO_AI = ("C1",)
CRYPTO_NOT_BUILT: tuple[str, ...] = ()

TEMPLATES = {a: templates.load(a) for a in templates.TEMPLATE_FILES}   # agents/prompts/ (§8)


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


# §8 Claude reasoning gate (v1.14): during the shakedown, one untraded shadow call per equity
# decision day, Claude at effort high on exactly agent 8's prompt. Its own client and record.
SHADOW_OF = "8"
SHADOW_REASONING = "high"
SHADOW_EXPERIMENT = "shakedown"
SHADOW_START, SHADOW_END = date(2026, 12, 14), date(2027, 1, 8)
SHADOW_KEY = "shadow:8"                 # the client_for key and heartbeat name ('decide:shadow:8')


def shadow_active(experiment: str, asset_class: str, day: date) -> bool:
    return (experiment == SHADOW_EXPERIMENT and asset_class == "us_equity"
            and SHADOW_START <= day <= SHADOW_END)


def shadow_agent(client: LLMClient, sleep: Callable[[float], None], backoff: Sequence[float] = BACKOFF_S) -> AIAgent:
    model = replace(model_for(SHADOW_OF), reasoning=SHADOW_REASONING)
    return AIAgent(SHADOW_OF, model, TEMPLATES[SHADOW_OF], client, sleep=sleep, backoff=backoff)
