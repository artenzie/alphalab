"""The OpenRouter client: one LLMClient for every model (protocol §8, v1.10; D8).

    client = OpenRouterClient()                       # key from OPENROUTER_API_KEY
    resp = client.complete(prompt, model=model_for(9))

Request: the model id; provider {"only": the official endpoints,
"allow_fallbacks": false, "require_parameters": true} so no other host serves
it and no parameter is silently dropped; max_tokens (16,000); reasoning effort
(medium); temperature only where the model takes one; usage + cost included.

Response -> LLMResponse: text, provider that served it, model id reported,
tokens in/out (+ reasoning tokens), cost in USD, latency, finish reason. Whether
the provider and model are the pinned ones is checked by the harness
(AIAgent), which logs both; this client also logs a warning on a mismatch.

Errors:
  * 402, or a message about credits / spend or key limits -> LLMCapped
  * timeout, connection error, 408, 429, 5xx                -> LLMUnavailable
  * anything else (400 bad parameter, 401, 403, 404, a malformed reply)
    -> OpenRouterError with the status code: a bug, never retried.
An error OpenRouter puts in a 200 body ({"error": {"code": ...}}) is mapped
the same way by its code.

The API key is read once from the environment, sent only in the
Authorization header and never printed, logged or put in an exception: every
message passes through _scrub, and transport exceptions aren't chained.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Callable

import requests

from agents.llm import LLMCapped, LLMResponse, LLMUnavailable, model_matches
from agents.models import ModelConfig

URL = "https://openrouter.ai/api/v1/chat/completions"
KEY_ENV = "OPENROUTER_API_KEY"
TIMEOUT_S = (10.0, 300.0)            # connect, read: a medium-reasoning reply can take minutes
MESSAGE_CHARS = 300

log = logging.getLogger(__name__)

_CAP_WORDS = re.compile(r"credit|spend(ing)? limit|key limit|budget|limit exceeded|insufficient", re.I)
_TRANSPORT = (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError)


class OpenRouterError(RuntimeError):
    """A request OpenRouter rejected for a reason that retrying won't fix (a bug)."""

    def __init__(self, status: int | None, message: str):
        super().__init__(f"OpenRouter {status}: {message}")
        self.status = status


class OpenRouterClient:
    """LLMClient over OpenRouter's chat completions API. session/clock are injectable for tests."""

    def __init__(self, session: Any = None, clock: Callable[[], float] = time.monotonic,
                 timeout: tuple[float, float] = TIMEOUT_S):
        key = os.getenv(KEY_ENV)
        if not key:
            raise RuntimeError(f"{KEY_ENV} is not set")
        self.__key = key
        self._session = session if session is not None else requests.Session()
        self._clock = clock
        self._timeout = timeout

    def __repr__(self) -> str:
        return "OpenRouterClient(key=[REDACTED])"

    # -- the request --

    @staticmethod
    def body(prompt: str, model: ModelConfig) -> dict:
        body = {
            "model": model.model_id,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": model.max_tokens,
            "provider": {"only": list(model.endpoints), "allow_fallbacks": False, "require_parameters": True},
            "usage": {"include": True},
        }
        if model.reasoning != "default":
            body["reasoning"] = {"effort": model.reasoning}
        if model.temperature is not None:
            body["temperature"] = model.temperature
        return body

    def complete(self, prompt: str, *, model: ModelConfig) -> LLMResponse:
        headers = {"Authorization": f"Bearer {self.__key}", "Content-Type": "application/json"}
        t0 = self._clock()
        try:
            r = self._session.post(URL, headers=headers, json=self.body(prompt, model), timeout=self._timeout)
        except _TRANSPORT as e:
            msg = f"{type(e).__name__}: {self._scrub(str(e))}"
            log.warning("%s: transport error %s", model.model_id, msg)
            raise LLMUnavailable(msg) from None
        except requests.RequestException as e:
            raise OpenRouterError(None, f"{type(e).__name__}: {self._scrub(str(e))}") from None
        status = r.status_code
        try:
            data = r.json()
        except ValueError:
            data = None
        latency_ms = max(0, round((self._clock() - t0) * 1000))

        if status != 200:
            self._raise(status, self._message(data, r), model)
        if not isinstance(data, dict):
            raise OpenRouterError(status, "reply is not a JSON object")
        if "error" in data:
            err = data["error"] if isinstance(data["error"], dict) else {}
            code = err.get("code")
            self._raise(code if isinstance(code, int) else None, self._message(data, r), model)
        return self._parse(data, model, latency_ms)

    # -- the response --

    def _parse(self, data: dict, model: ModelConfig, latency_ms: int) -> LLMResponse:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise OpenRouterError(200, "reply has no choices")
        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content")
        if isinstance(text, list):          # content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        if not isinstance(text, str):
            text = ""                      # e.g. all output spent on reasoning: the harness marks it invalid
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
        if not usage:
            log.warning("%s: reply without usage; tokens and cost not recorded", model.model_id)
        provider, reported = data.get("provider"), data.get("model")
        if provider not in model.provider_names or not model_matches(model.model_id, reported):
            log.warning("%s: pinned to %s, served by %r reporting model %r",
                        model.model_id, "/".join(model.provider_names), provider, reported)
        resp = LLMResponse(
            text=text,
            model_id=reported if isinstance(reported, str) else "",
            provider_served=provider if isinstance(provider, str) else "",
            tokens_in=_int(usage.get("prompt_tokens")),
            tokens_out=_int(usage.get("completion_tokens")),
            cost_usd=_float(usage.get("cost")),
            latency_ms=latency_ms,
            reasoning_tokens=_int(details.get("reasoning_tokens")),
            finish_reason=choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else None,
        )
        log.info("%s: served by %s as %s, %s in / %s out, $%s, %d ms, finish %s", model.model_id,
                 resp.provider_served, resp.model_id, resp.tokens_in, resp.tokens_out, resp.cost_usd,
                 latency_ms, resp.finish_reason)
        return resp

    # -- errors --

    def _raise(self, status: int | None, message: str, model: ModelConfig):
        log.warning("%s: OpenRouter %s: %s", model.model_id, status, message)
        if status == 402 or (status is not None and 400 <= status < 500 and _CAP_WORDS.search(message)):
            raise LLMCapped(f"{status}: {message}")
        if status in (408, 429) or (status is not None and status >= 500):
            raise LLMUnavailable(f"{status}: {message}")
        raise OpenRouterError(status, message)

    def _message(self, data: Any, r: Any) -> str:
        msg = None
        if isinstance(data, dict):
            err = data.get("error")
            msg = err.get("message") if isinstance(err, dict) else err
            if isinstance(err, dict) and isinstance(err.get("metadata"), dict):
                raw = err["metadata"].get("raw") or err["metadata"].get("provider_name")
                if raw:
                    msg = f"{msg} ({raw})"
        if not msg:
            msg = getattr(r, "text", "") or ""
        return self._scrub(" ".join(str(msg).split()))[:MESSAGE_CHARS]

    def _scrub(self, text: str) -> str:
        return text.replace(self.__key, "[REDACTED]") if self.__key else text


def _int(x) -> int | None:
    return x if isinstance(x, int) and not isinstance(x, bool) and x >= 0 else None


def _float(x) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0 else None
