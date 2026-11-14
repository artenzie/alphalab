"""Agent id -> decide function (protocol §5 numbering)."""

from __future__ import annotations

from types import MappingProxyType

from strategies import benchmark, equal_weight
from strategies.base import Agent

AGENTS: MappingProxyType[int, Agent] = MappingProxyType({
    benchmark.AGENT_ID: benchmark.decide,
    equal_weight.AGENT_ID: equal_weight.decide,
})

NAMES: MappingProxyType[int, str] = MappingProxyType({
    benchmark.AGENT_ID: benchmark.NAME,
    equal_weight.AGENT_ID: equal_weight.NAME,
})


def get_agent(agent_id: int) -> Agent:
    try:
        return AGENTS[agent_id]
    except KeyError:
        raise KeyError(f"no agent with id {agent_id}; known: {sorted(AGENTS)}") from None
