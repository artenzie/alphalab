"""The one client interface every model is called through (protocol §8).

    response = client.complete(prompt, model=model_for(8))     # a ModelConfig (agents/models.py)

A client sends one prompt to one pinned model and returns the text plus what
the decisions table logs: the model id the provider reports, the provider that
actually served the call, tokens in and out, cost and latency (plus reasoning
tokens and the finish reason, when reported). It raises LLMUnavailable for a
transport or provider error (the harness retries) and LLMCapped when the spend
cap is reached (the harness holds without retrying). It never substitutes
another model or provider.

model_matches(requested, reported) is the §8 model id check: the reported id
must be the requested one or a dated variant of it.

FakeClient is the test double: a scripted list of responses and exceptions,
with no I/O. The real client is agents/openrouter.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, Sequence

from agents.models import ModelConfig


class LLMUnavailable(Exception):
    """The model or provider could not be reached or returned an error."""


class LLMCapped(Exception):
    """The spend cap is reached (§8: hold, log as 'capped')."""


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model_id: str
    provider_served: str
    tokens_in: int | None                    # None only if the provider didn't report usage
    tokens_out: int | None
    cost_usd: float | None
    latency_ms: int
    reasoning_tokens: int | None = None      # part of tokens_out, when the provider reports it
    finish_reason: str | None = None         # 'stop', 'length', ...


class LLMClient(Protocol):
    def complete(self, prompt: str, *, model: ModelConfig) -> LLMResponse: ...


# A dated variant: the requested id plus a date suffix, e.g. -20261013, -2026-10-13, -1013, @20261013.
_DATED = re.compile(r"(?:-\d{8}|-\d{4}-\d{2}-\d{2}|-\d{4}|@\d{8})")


def model_matches(requested: str, reported: str | None) -> bool:
    """True if the reported model id is the requested one or a dated variant of it (§8)."""
    if not isinstance(reported, str) or not reported:
        return False
    if reported == requested:
        return True
    return reported.startswith(requested) and _DATED.fullmatch(reported[len(requested):]) is not None


class FakeClient:
    """Returns (or raises) the scripted items in order; records every call."""

    def __init__(self, script: Sequence[LLMResponse | BaseException]):
        self._script = list(script)
        self.calls: list[dict] = []

    def complete(self, prompt: str, *, model: ModelConfig) -> LLMResponse:
        self.calls.append({"prompt": prompt, "model": model})
        if not self._script:
            raise AssertionError("FakeClient script exhausted")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def prompts(self) -> list[str]:
        return [c["prompt"] for c in self.calls]
