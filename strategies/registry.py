"""Agent id -> decide function (protocol §5 numbering)."""

from __future__ import annotations

from types import MappingProxyType

from strategies import benchmark, equal_weight, long_short, mean_reversion, momentum, trend
from strategies.base import Agent

_MODULES = (benchmark, equal_weight, momentum, mean_reversion, trend, long_short)

AGENTS: MappingProxyType[int, Agent] = MappingProxyType({m.AGENT_ID: m.decide for m in _MODULES})
NAMES: MappingProxyType[int, str] = MappingProxyType({m.AGENT_ID: m.NAME for m in _MODULES})
assert len(AGENTS) == len(_MODULES), "duplicate agent id"


def get_agent(agent_id: int) -> Agent:
    try:
        return AGENTS[agent_id]
    except KeyError:
        raise KeyError(f"no agent with id {agent_id}; known: {sorted(AGENTS)}") from None
