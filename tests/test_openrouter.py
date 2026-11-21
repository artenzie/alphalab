"""The OpenRouter client (agents/openrouter.py): protocol §8 (v1.10).

A fake HTTP session only: no network. The key is a sentinel set in the
environment; no test may see it in an exception, a log line or a repr.
"""

import ast
import json
import logging
import sys
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agents.harness import AIAgent  # noqa: E402
from agents.templates import load as load_template  # noqa: E402

TEMPLATE = load_template(8)
from agents.llm import LLMCapped, LLMUnavailable  # noqa: E402
from agents.models import CLAUDE, GEMINI, GPT, GROK, ModelConfig  # noqa: E402
from agents.openrouter import KEY_ENV, URL, OpenRouterClient, OpenRouterError  # noqa: E402
from test_harness import HOLDING, REBAL, context, D  # noqa: E402
from test_strategies import make_snapshot  # noqa: E402

KEY = "sk-or-v1-SENTINEL-0123456789abcdef"


class FakeResponse:
    def __init__(self, status, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else ("" if body is None else json.dumps(body))

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


class FakeSession:
    def __init__(self, *items):
        self.items = list(items)
        self.posts = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def ok_body(model=CLAUDE.model_id, provider="Anthropic", content=REBAL, finish="stop", usage=True):
    body = {"id": "gen-1", "model": model, "provider": provider,
            "choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": content}}]}
    if usage:
        body["usage"] = {"prompt_tokens": 5123, "completion_tokens": 812, "cost": 0.02755,
                         "completion_tokens_details": {"reasoning_tokens": 600}}
    return body


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)


@pytest.fixture(autouse=True)
def logs(caplog):
    caplog.set_level(logging.DEBUG, logger="agents.openrouter")
    yield caplog
    assert KEY not in caplog.text and "SENTINEL" not in caplog.text


def client_with(*items, clock=None):
    session = FakeSession(*items)
    ticks = iter(clock or [100.0, 101.25])
    return OpenRouterClient(session=session, clock=lambda: next(ticks)), session


def raised(exc_type, *items, model=CLAUDE):
    client, _ = client_with(*items)
    with pytest.raises(exc_type) as e:
        client.complete("prompt", model=model)
    text = str(e.value) + repr(e.value)
    assert KEY not in text and "SENTINEL" not in text
    assert e.value.__cause__ is None                                        # nothing chained
    assert e.value.__context__ is None or e.value.__suppress_context__
    return e.value


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

def test_request_pins_provider_without_fallbacks():
    client, session = client_with(FakeResponse(200, ok_body()))
    client.complete("hello", model=CLAUDE)
    post = session.posts[0]
    assert post["url"] == URL and post["headers"]["Authorization"] == f"Bearer {KEY}"
    body = post["json"]
    assert body["model"] == "anthropic/claude-sonnet-5.5"
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["provider"] == {"only": ["anthropic"], "allow_fallbacks": False, "require_parameters": True}
    assert body["reasoning"] == {"effort": "medium"}
    assert body["max_tokens"] == 16_000
    assert body["usage"] == {"include": True}
    assert "temperature" not in body                      # Claude takes none: provider default
    assert set(body) == {"model", "messages", "provider", "reasoning", "max_tokens", "usage"}


@pytest.mark.parametrize("model", [GEMINI, GROK])
def test_temperature_zero_where_accepted(model):
    body = OpenRouterClient.body("p", model)
    assert body["temperature"] == 0.0 and body["reasoning"] == {"effort": "medium"}


def test_gemini_pins_both_google_endpoints():
    assert OpenRouterClient.body("p", GEMINI)["provider"]["only"] == ["google-ai-studio", "google-vertex"]


def test_default_reasoning_is_left_out():
    cfg = ModelConfig("x", "a/b", ("A",), ("a",), None, reasoning="default")
    assert "reasoning" not in OpenRouterClient.body("p", cfg)


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------

def test_success_parsing():
    client, _ = client_with(FakeResponse(200, ok_body()))
    r = client.complete("p", model=CLAUDE)
    assert r.text == REBAL and r.provider_served == "Anthropic" and r.model_id == CLAUDE.model_id
    assert (r.tokens_in, r.tokens_out, r.reasoning_tokens) == (5123, 812, 600)
    assert r.cost_usd == pytest.approx(0.02755) and r.latency_ms == 1250 and r.finish_reason == "stop"


def test_null_content_becomes_empty_text():
    client, _ = client_with(FakeResponse(200, ok_body(content=None, finish="length")))
    r = client.complete("p", model=CLAUDE)
    assert r.text == "" and r.finish_reason == "length"


def test_missing_usage_is_recorded_as_none(logs):
    client, _ = client_with(FakeResponse(200, ok_body(usage=False)))
    r = client.complete("p", model=CLAUDE)
    assert (r.tokens_in, r.tokens_out, r.cost_usd, r.reasoning_tokens) == (None, None, None, None)
    assert "without usage" in logs.text


def test_provider_and_model_mismatch_returned_and_logged(logs):
    client, _ = client_with(FakeResponse(200, ok_body(model="anthropic/claude-haiku-5.5", provider="Amazon Bedrock")))
    r = client.complete("p", model=CLAUDE)
    assert (r.provider_served, r.model_id) == ("Amazon Bedrock", "anthropic/claude-haiku-5.5")
    assert "served by 'Amazon Bedrock' reporting model 'anthropic/claude-haiku-5.5'" in logs.text
    assert CLAUDE.model_id in logs.text


def test_dated_variant_not_logged_as_mismatch(logs):
    client, _ = client_with(FakeResponse(200, ok_body(model=CLAUDE.model_id + "-20261013")))
    client.complete("p", model=CLAUDE)
    assert "pinned to" not in logs.text


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

def err(code, message, **extra):
    return {"error": {"code": code, "message": message, **extra}}


def test_402_is_capped():
    raised(LLMCapped, FakeResponse(402, err(402, "Insufficient credits")))


def test_spend_limit_message_is_capped():
    raised(LLMCapped, FakeResponse(403, err(403, "Key limit exceeded")))


def test_402_in_a_200_body_is_capped():
    raised(LLMCapped, FakeResponse(200, err(402, "This request requires more credits")))


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_rate_limit_and_server_errors_are_unavailable(status):
    e = raised(LLMUnavailable, FakeResponse(status, err(status, "No endpoints available")))
    assert str(status) in str(e)


def test_5xx_without_json_is_unavailable():
    raised(LLMUnavailable, FakeResponse(503, None, text="<html>Service Unavailable</html>"))


def test_503_in_a_200_body_is_unavailable():
    raised(LLMUnavailable, FakeResponse(200, err(503, "provider returned error")))


@pytest.mark.parametrize("exc", [requests.Timeout("read timed out"), requests.ConnectionError("refused"),
                                 requests.exceptions.ChunkedEncodingError("broken")])
def test_transport_errors_are_unavailable(exc):
    raised(LLMUnavailable, exc)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_other_errors_raise_with_status(status):
    e = raised(OpenRouterError, FakeResponse(status, err(status, "temperature is not supported")))
    assert e.status == status and f"OpenRouter {status}" in str(e) and "temperature is not supported" in str(e)


def test_400_is_a_bug_not_retried_by_the_harness():
    client, session = client_with(FakeResponse(400, err(400, "bad parameter")))
    agent = AIAgent(8, CLAUDE, TEMPLATE, client, sleep=lambda s: pytest.fail("slept"))
    with pytest.raises(OpenRouterError):
        agent(make_snapshot(D), HOLDING, context())
    assert len(session.posts) == 1


def test_malformed_200_raises():
    raised(OpenRouterError, FakeResponse(200, None, text="not json"))
    raised(OpenRouterError, FakeResponse(200, {"choices": []}))


def test_other_request_exception_raises_as_bug():
    raised(OpenRouterError, requests.exceptions.InvalidURL("bad url"))


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("item,exc_type", [
    (FakeResponse(401, err(401, f"Invalid key {KEY}")), OpenRouterError),
    (FakeResponse(402, None, text=f"no credits for Bearer {KEY}"), LLMCapped),
    (FakeResponse(503, err(503, "down", metadata={"raw": f"echo {KEY}"})), LLMUnavailable),
    (requests.ConnectionError(f"failed with header Bearer {KEY}"), LLMUnavailable),
    (requests.exceptions.InvalidHeader(f"bad header {KEY}"), OpenRouterError),
])
def test_key_never_in_exceptions_or_logs(item, exc_type):
    e = raised(exc_type, item)
    assert "[REDACTED]" in str(e)


def test_key_not_in_repr_or_attributes():
    client, _ = client_with()
    assert KEY not in repr(client) and KEY not in str(vars(client).get("_session"))
    assert not any(v == KEY for k, v in vars(client).items() if not k.endswith("__key"))


def test_missing_key_fails_without_value(monkeypatch):
    monkeypatch.delenv(KEY_ENV)
    with pytest.raises(RuntimeError, match=KEY_ENV):
        OpenRouterClient(session=FakeSession())


def test_module_reads_key_only_via_getenv_and_never_prints():
    src = (ROOT / "agents" / "openrouter.py").read_text()
    tree = ast.parse(src)
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "print" not in calls and "open" not in calls
    assert "dotenv" not in src and "os.environ" not in src and src.count("os.getenv(") == 1
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported <= {"__future__", "logging", "os", "re", "time", "typing", "requests",
                        "agents.llm", "agents.models"}, imported


# ---------------------------------------------------------------------------
# End to end through the harness
# ---------------------------------------------------------------------------

def test_through_aiagent_ok():
    client, _ = client_with(FakeResponse(200, ok_body(model="openai/gpt-6.1-sol-20261101", provider="OpenAI")))
    dec = AIAgent(9, GPT, TEMPLATE, client)(make_snapshot(D), HOLDING, context())
    row = dec.record.attempts[0].row()
    assert dec.record.status == "ok" and dict(dec.weights) == {"AAPL": 0.2, "NVDA": -0.1}
    assert (row["model_id"], row["provider_served"], row["reasoning_setting"], row["temperature"]) == \
        ("openai/gpt-6.1-sol-20261101", "OpenAI", "medium", None)
    assert (row["tokens_in"], row["tokens_out"], row["reasoning_tokens"], row["latency_ms"]) == (5123, 812, 600, 1250)


def test_through_aiagent_wrong_model_twice_holds_invalid():
    bad = ok_body(model="anthropic/claude-haiku-5.5")
    client, session = client_with(FakeResponse(200, bad), FakeResponse(200, bad), clock=[0, 1, 2, 3])
    dec = AIAgent(8, CLAUDE, TEMPLATE, client)(make_snapshot(D), HOLDING, context())
    assert dec.is_hold and dec.record.status == "invalid" and len(session.posts) == 2
    assert all("wrong model" in a.error for a in dec.record.attempts)


def test_through_aiagent_429_retries_then_holds_unavailable():
    client, session = client_with(*[FakeResponse(429, err(429, "rate limited"))] * 4, clock=list(range(8)))
    sleeps = []
    dec = AIAgent(8, CLAUDE, TEMPLATE, client, sleep=sleeps.append)(make_snapshot(D), HOLDING, context())
    assert dec.record.status == "unavailable" and sleeps == [300, 600, 900] and len(session.posts) == 4
