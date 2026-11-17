"""Agent id -> decide function (protocol §5 numbering; crypto side agents §13)."""

from __future__ import annotations

from types import MappingProxyType

from strategies import benchmark, bitcoin, equal_weight, long_short, mean_reversion, momentum, trend
from strategies.base import Agent

_MODULES = (benchmark, equal_weight, momentum, mean_reversion, trend, long_short)
_CRYPTO_MODULES = (bitcoin,)

AGENTS: MappingProxyType[int, Agent] = MappingProxyType({m.AGENT_ID: m.decide for m in _MODULES})
NAMES: MappingProxyType[int, str] = MappingProxyType({m.AGENT_ID: m.NAME for m in _MODULES})
assert len(AGENTS) == len(_MODULES), "duplicate agent id"

# Rule-based crypto agents (C0), keyed by their string id; C1 is an AI agent (agents/harness.py).
CRYPTO_AGENTS: MappingProxyType[str, Agent] = MappingProxyType({m.AGENT_ID: m.decide for m in _CRYPTO_MODULES})
CRYPTO_NAMES: MappingProxyType[str, str] = MappingProxyType({m.AGENT_ID: m.NAME for m in _CRYPTO_MODULES})


def get_agent(agent_id: int | str) -> Agent:
    if isinstance(agent_id, str) and agent_id in CRYPTO_AGENTS:
        return CRYPTO_AGENTS[agent_id]
    try:
        return AGENTS[agent_id]
    except KeyError:
        raise KeyError(f"no agent with id {agent_id}; known: {sorted(AGENTS)} + {sorted(CRYPTO_AGENTS)}") from None
