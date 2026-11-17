"""The frozen AI model lineup (protocol §8, v1.10) as data: the single source of truth.

    from agents.models import model_for
    cfg = model_for(8)          # ModelConfig for agent 8 (Claude)

Every model is called through OpenRouter, pinned to its maker's standard
endpoint with fallbacks off. Two kinds of provider name:
  * endpoints: the OpenRouter endpoint slugs sent in provider.only (pinning);
  * provider_names: the names OpenRouter reports as the provider that served
    the call. Any other name makes the call invalid (§8).
Reasoning effort is medium for all five (v1.10, so the comparison isn't
reasoning vs non-reasoning). Temperature is 0 where the model accepts it;
Claude and GPT accept none, so it is not sent (None = provider default).
Max output is 16,000 tokens per call, reasoning included.

tests/test_models.py checks this against the §8 table. Rule 3: never change
after the official start date (Mon 11 Jan 2027).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

MAX_TOKENS = 16_000
REASONING = "medium"


@dataclass(frozen=True)
class ModelConfig:
    name: str                          # short label: claude, gpt, gemini, deepseek, grok
    model_id: str                      # OpenRouter model id
    provider_names: tuple[str, ...]    # provider names OpenRouter reports for the official provider
    endpoints: tuple[str, ...]         # endpoint slugs pinned in provider.only
    temperature: float | None          # None = not sent; the provider default applies
    reasoning: str = REASONING         # reasoning effort sent to OpenRouter
    max_tokens: int = MAX_TOKENS


CLAUDE = ModelConfig("claude", "anthropic/claude-sonnet-5.5", ("Anthropic",), ("anthropic",), None)
GPT = ModelConfig("gpt", "openai/gpt-6.1-sol", ("OpenAI",), ("openai",), None)
GEMINI = ModelConfig("gemini", "google/gemini-4-carbon", ("Google AI Studio", "Google"),
                     ("google-ai-studio", "google-vertex"), 0.0)
DEEPSEEK = ModelConfig("deepseek", "deepseek/deepseek-v4-pro-0813", ("DeepSeek",), ("deepseek",), 0.0)
GROK = ModelConfig("grok", "x-ai/grok-4.7", ("xAI",), ("xai",), 0.0)

MODELS: Mapping[str, ModelConfig] = MappingProxyType(
    {m.name: m for m in (CLAUDE, GPT, GEMINI, DEEPSEEK, GROK)})

# Agents 7, 13, 14, 15 and C1 use agent 8's Claude model (§8).
AGENT_MODELS: Mapping[str, ModelConfig] = MappingProxyType({
    "7": CLAUDE, "8": CLAUDE, "9": GPT, "10": GEMINI, "11": DEEPSEEK, "12": GROK,
    "13": CLAUDE, "14": CLAUDE, "15": CLAUDE, "C1": CLAUDE,
})


def model_for(agent_id: int | str) -> ModelConfig:
    key = str(agent_id)
    try:
        return AGENT_MODELS[key]
    except KeyError:
        raise KeyError(f"agent {agent_id!r} is not an AI agent; AI agents: {sorted(AGENT_MODELS)}") from None
