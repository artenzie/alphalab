"""The one client interface every model is called through (protocol §8).

    response = client.complete(prompt, model_id=..., provider=..., temperature=..., reasoning=...)

A client sends one prompt to one pinned model and returns the text plus what
the decisions table logs: the model id, the provider that actually served the
call, tokens in and out, cost and latency. It raises LLMUnavailable for a
transport or provider error (the harness retries) and LLMCapped when the spend
cap is reached (the harness holds without retrying). It never substitutes
another model or provider.

FakeClient is the test double: a scripted list of responses and exceptions,
with no I/O. The OpenRouter client is a separate module (next session).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


class LLMUnavailable(Exception):
    """The model or provider could not be reached or returned an error."""


class LLMCapped(Exception):
    """The spend cap is reached (§8: hold, log as 'capped')."""


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model_id: str
    provider_served: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int


class LLMClient(Protocol):
    def complete(self, prompt: str, *, model_id: str, provider: str,
                 temperature: float | None, reasoning: str) -> LLMResponse: ...


class FakeClient:
    """Returns (or raises) the scripted items in order; records every call."""

    def __init__(self, script: Sequence[LLMResponse | BaseException]):
        self._script = list(script)
        self.calls: list[dict] = []

    def complete(self, prompt: str, *, model_id: str, provider: str,
                 temperature: float | None, reasoning: str) -> LLMResponse:
        self.calls.append({"prompt": prompt, "model_id": model_id, "provider": provider,
                           "temperature": temperature, "reasoning": reasoning})
        if not self._script:
            raise AssertionError("FakeClient script exhausted")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def prompts(self) -> list[str]:
        return [c["prompt"] for c in self.calls]
