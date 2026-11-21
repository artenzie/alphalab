"""The AI agent harness: one interface every model plugs into (protocol §4, §5, §8, v1.10).

    agent = AIAgent(agent_id=8, model=model_for(8), template=templates.load(8), client=client)
    decision = agent(snapshot, portfolio, context)      # same call as agents 1-6
    for attempt in decision.record.attempts:             # one decisions row per call
        row = attempt.row()

An AIAgent is an Agent like the rule-based ones: the runner calls it with the
snapshot, the agent's own portfolio and the context, and gets a Decision. It
returns an AIDecision, a Decision that also carries the call record: status,
and one AttemptRecord per call with every AI column of the decisions table.
The runner adds agent_id, snapshot_id and decided_at and writes the rows; the
harness never touches the database, files, network or environment itself.

The prompt is: the agent's fixed template (agents/templates.py) + OUTPUT_SPEC + the input as compact
JSON (date, snapshot fields, own portfolio, own limits, own last 5 decisions, and
run: the end date and trading days left, v1.15).
The AI inputs a Portfolio doesn't carry (average cost, starting value, past
decisions) come in on context.memory, set by the runner.

Calls (§8):
  * LLMUnavailable -> 3 retries, waiting 5, 10 and 15 minutes; still down ->
    hold, status 'unavailable'. Never another model or provider.
  * LLMCapped -> hold, status 'capped', no retry.
  * served by a provider not in model.provider_names, or reporting a model id
    that isn't the requested one or a dated variant of it -> that attempt is
    'invalid' (the row logs the reported id; the error names both).
  * a reply cut off at max output (finish_reason 'length') -> 'invalid'.
  * invalid output -> one retry with the validation error shown (a wrong
    provider or model is retried with the original prompt); still invalid ->
    hold, status 'invalid'.

Valid output (v1.8 rule 1, v1.9):
  {"action": "hold", "reasoning": "..."}
  {"action": "rebalance", "targets": {"AAPL": 0.1, ...}, "reasons": {"AAPL": "...", ...},
   "reasoning": "...", "confidence": 0.7}
Targets are the complete portfolio; only the 22 stocks (SPY is context only);
weights finite numbers; a non-empty reason per target; confidence in [0, 1].
Weight limits are not checked here: the §6 risk checks clip them afterwards.

Crypto (C1, §13, v1.12): the same harness on a crypto snapshot. The output
spec (OUTPUT_SPEC_CRYPTO) names the 5 coins and says long only, the limits
key is max_per_coin, and an unknown symbol's error names the coins. Long only
and the 50% cap are enforced by the risk checks (profile 'C1'), not here. The
equity prompt is byte-for-byte what it was. A crypto agent (C0, C1) only
accepts a crypto snapshot and an equity agent only an equity one.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Callable, Mapping, Sequence

from agents.llm import LLMCapped, LLMClient, LLMResponse, LLMUnavailable, model_matches
from agents.models import ModelConfig
from execution.risk import profile
from strategies.base import (BENCHMARK_SYMBOL, Context, Decision, PastDecision, Portfolio,
                             stock_fields)

if TYPE_CHECKING:  # type hints only: the harness never imports the data layer at runtime
    from data.snapshot import Snapshot

BACKOFF_S = (300, 600, 900)      # 3 retries over 30 minutes (§8)
LAST_DECISIONS = 5               # §4
REASON_CHARS = 300               # Decision.reason is a one-line summary; full text is in the record

OUTPUT_SPEC = """\
OUTPUT FORMAT
Reply with one JSON object and nothing else (a ```json fenced block is also accepted). Either:
  {"action": "hold", "reasoning": "<why>"}
to keep your portfolio exactly as it is, or:
  {"action": "rebalance", "targets": {"<SYMBOL>": <weight>, ...}, "reasons": {"<SYMBOL>": "<one line>", ...}, "reasoning": "<overall reasoning>", "confidence": <0 to 1>}
Rules:
- targets is your COMPLETE target portfolio. Any stock you hold and leave out of targets is sold (a short is bought back). {} means all cash.
- A weight is a fraction of your current portfolio value; negative = short. Use only the 22 stocks in the snapshot.
- SPY is market context only: it can't be a target.
- Give a one-line reason for every symbol in targets.
- confidence is a number from 0 to 1.
- Your limits are in the input; positions beyond them are cut back pro rata."""

OUTPUT_SPEC_CRYPTO = """\
OUTPUT FORMAT
Reply with one JSON object and nothing else (a ```json fenced block is also accepted). Either:
  {"action": "hold", "reasoning": "<why>"}
to keep your portfolio exactly as it is, or:
  {"action": "rebalance", "targets": {"<SYMBOL>": <weight>, ...}, "reasons": {"<SYMBOL>": "<one line>", ...}, "reasoning": "<overall reasoning>", "confidence": <0 to 1>}
Rules:
- targets is your COMPLETE target portfolio. Any coin you hold and leave out of targets is sold. {} means all cash.
- A weight is a fraction of your current portfolio value. Long only: no negative weights. Use only the 5 coins in the snapshot, written as in the snapshot (e.g. "BTC/USD").
- Give a one-line reason for every symbol in targets.
- confidence is a number from 0 to 1.
- Your limits are in the input; positions beyond them are cut back pro rata."""

CRYPTO_AGENT_IDS = frozenset({"C0", "C1"})

RETRY_NOTE = ("\n\nYOUR PREVIOUS REPLY WAS INVALID: {error}\n"
              "Reply again with one valid JSON object, following the OUTPUT FORMAT above.")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _compact(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=True)


def _r(x: float) -> float:
    return round(float(x), 6)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def _past(d: PastDecision) -> dict:
    return {"date": d.decision_date.isoformat(), "status": d.status,
            "action": "hold" if d.targets is None else "rebalance",
            "targets": None if d.targets is None else {s: _r(w) for s, w in d.targets.items()},
            "reasons": dict(d.reasons), "reasoning": d.reasoning}


def _check_asset_class(agent_id: int | str, snapshot: "Snapshot") -> str:
    want = "crypto" if str(agent_id) in CRYPTO_AGENT_IDS else "us_equity"
    if snapshot.asset_class != want:
        raise ValueError(f"agent {agent_id} decides on {want} snapshots, got {snapshot.asset_class}")
    return want


def prompt_input(agent_id: int | str, snapshot: "Snapshot", portfolio: Portfolio, context: Context) -> dict:
    """The §4 / D5 (§13 for crypto) inputs of an AI agent, and nothing else."""
    crypto = _check_asset_class(agent_id, snapshot) == "crypto"
    if snapshot.decision_date != context.decision_date.isoformat():
        raise ValueError(f"snapshot is for {snapshot.decision_date}, context for {context.decision_date}")
    memory = context.memory
    if memory is None:
        raise ValueError("an AI agent needs context.memory (set by the runner)")
    if memory.end_date is None or memory.trading_days_left is None:
        raise ValueError("an AI agent needs the run's end date and trading days left (context.memory, v1.15)")
    if memory.end_date < context.decision_date or memory.trading_days_left < 0:
        raise ValueError(f"decision date {context.decision_date} is after the run's end date {memory.end_date}")
    past = sorted(memory.last_decisions, key=lambda d: d.decision_date)
    if past and past[-1].decision_date >= context.decision_date:
        raise ValueError("last_decisions must be before the decision date")
    symbols = snapshot.payload["symbols"]
    positions = {}
    for sym, shares in sorted(portfolio.positions.items()):
        if shares == 0:
            continue
        close = symbols.get(sym, {}).get("last_close")
        avg = memory.avg_cost.get(sym)
        positions[sym] = {
            "shares": _r(shares),
            "last_close": close,
            "weight": _r(shares * close / portfolio.value) if close is not None and portfolio.value else None,
            "avg_cost": None if avg is None else _r(avg),
            "unrealised_pnl": _r(shares * (close - avg)) if close is not None and avg is not None else None,
        }
    lim = profile(agent_id)
    return {
        "date": context.decision_date.isoformat(),
        "snapshot": symbols,
        "portfolio": {
            "cash": _r(portfolio.cash),
            "value": _r(portfolio.value),
            "return_since_start": _r(portfolio.value / memory.starting_value - 1) if memory.starting_value else None,
            "positions": positions,
        },
        "limits": {("max_per_coin" if crypto else "max_per_stock"): lim.max_per_stock, "shorts_allowed": lim.shorts,
                   "max_gross": lim.max_gross, "max_total_short": lim.max_total_short},
        "last_decisions": [_past(d) for d in past[-LAST_DECISIONS:]],
        "run": {"end_date": memory.end_date.isoformat(), "trading_days_left": int(memory.trading_days_left)},
    }


def build_prompt(template: str, agent_id: int | str, snapshot: "Snapshot", portfolio: Portfolio,
                 context: Context) -> str:
    spec = OUTPUT_SPEC_CRYPTO if _check_asset_class(agent_id, snapshot) == "crypto" else OUTPUT_SPEC
    return (template.rstrip() + "\n\n" + spec + "\n\nINPUT\n"
            + _compact(prompt_input(agent_id, snapshot, portfolio, context)))


# ---------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------

class OutputError(ValueError):
    """The model's reply is not a valid decision; the message is shown to it on the retry."""


@dataclass(frozen=True)
class Parsed:
    targets: Mapping[str, float] | None      # None = hold
    reasons: Mapping[str, str] | None
    reasoning: str
    confidence: float | None


_FENCE = re.compile(r"```(?:json|JSON)?[ \t]*\n?(.*?)```", re.DOTALL)


def _no_constants(name: str):
    raise OutputError(f"{name} is not a valid number")


def _no_dupes(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise OutputError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _number(x) -> bool:
    return not isinstance(x, bool) and isinstance(x, (int, float)) and math.isfinite(x)


def parse_and_validate(text: str, universe: Sequence[str], asset_class: str = "us_equity") -> Parsed:
    """Parse the reply and check it against the §8 format. Raises OutputError."""
    if not isinstance(text, str) or not text.strip():
        raise OutputError("empty reply")
    body = text.strip()
    if not body.startswith("{"):
        fences = _FENCE.findall(body)
        if len(fences) != 1:
            raise OutputError("reply must be one JSON object, bare or in one ```json block"
                              + (f" (found {len(fences)} blocks)" if fences else ""))
        body = fences[0].strip()
    try:
        obj = json.loads(body, parse_constant=_no_constants, object_pairs_hook=_no_dupes)
    except OutputError:
        raise
    except ValueError as e:
        raise OutputError(f"not valid JSON: {e}") from None
    if not isinstance(obj, dict):
        raise OutputError("reply must be a JSON object")
    action = obj.get("action")
    reasoning = obj.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise OutputError("reasoning must be a non-empty string")
    confidence = obj.get("confidence")
    if confidence is not None and not (_number(confidence) and 0 <= confidence <= 1):
        raise OutputError(f"confidence must be a number from 0 to 1, got {confidence!r}")
    if action == "hold":
        if "targets" in obj:
            raise OutputError("a hold has no targets; use action 'rebalance' to change the portfolio")
        return Parsed(None, None, reasoning, None if confidence is None else float(confidence))
    if action != "rebalance":
        raise OutputError(f"action must be 'hold' or 'rebalance', got {action!r}")
    targets, reasons = obj.get("targets"), obj.get("reasons")
    if not isinstance(targets, dict):
        raise OutputError("targets must be an object {symbol: weight}")
    if not isinstance(reasons, dict):
        raise OutputError("reasons must be an object {symbol: reason}")
    if confidence is None:
        raise OutputError("confidence is required for a rebalance")
    allowed = set(universe)
    for sym, w in targets.items():
        if sym == BENCHMARK_SYMBOL and asset_class == "us_equity":
            raise OutputError("SPY is context only; it can't be a target")
        if sym not in allowed:
            what = "the 5 coins" if asset_class == "crypto" else "the 22 stocks"
            raise OutputError(f"unknown symbol {sym!r}; use only {what} in the snapshot")
        if not _number(w):
            raise OutputError(f"weight for {sym} must be a finite number, got {w!r}")
        r = reasons.get(sym)
        if not isinstance(r, str) or not r.strip():
            raise OutputError(f"missing reason for {sym}")
    return Parsed(MappingProxyType({s: float(w) for s, w in targets.items()}),
                  MappingProxyType({s: reasons[s] for s in targets}), reasoning, float(confidence))


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AttemptRecord:
    """One call: the AI columns of one decisions row."""
    attempt: int
    status: str                         # 'ok', 'invalid', 'unavailable', 'capped'
    model_id: str
    reasoning_setting: str
    temperature: float | None
    prompt_hash: str
    provider_served: str | None = None
    raw_response: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    parsed: Parsed | None = None
    error: str | None = None
    reasoning_tokens: int | None = None  # part of tokens_out (§8 v1.11)

    def row(self) -> dict:
        """decisions columns; the runner adds agent_id, snapshot_id and decided_at.

        A valid hold stores parsed_targets as the JSON text 'null' (not SQL NULL),
        so the schema's "status 'ok' needs parsed_targets" check holds.
        """
        p = self.parsed
        return {
            "attempt": self.attempt, "status": self.status, "model_id": self.model_id,
            "provider_served": self.provider_served, "reasoning_setting": self.reasoning_setting,
            "temperature": self.temperature, "prompt_hash": self.prompt_hash,
            "raw_response": self.raw_response, "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out, "reasoning_tokens": self.reasoning_tokens,
            "cost_usd": self.cost_usd, "latency_ms": self.latency_ms,
            "parsed_targets": None if p is None else _compact(None if p.targets is None else dict(p.targets)),
            "reasons": None if p is None or p.reasons is None else _compact(dict(p.reasons)),
            "overall_reasoning": None if p is None else p.reasoning,
            "confidence": None if p is None else p.confidence,
            "error": self.error,
        }


@dataclass(frozen=True)
class DecisionRecord:
    status: str                          # the final status: the last attempt's
    attempts: tuple[AttemptRecord, ...]


@dataclass(frozen=True)
class AIDecision(Decision):
    """A Decision plus its call record (not part of equality, so it compares like any Decision)."""
    record: DecisionRecord | None = field(default=None, compare=False)


def _one_line(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= REASON_CHARS else line[:REASON_CHARS - 1] + "…"


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------

class _Stop(Exception):
    def __init__(self, status: str):
        self.status = status


class AIAgent:
    """An AI agent with the Agent interface: agent(snapshot, portfolio, context) -> AIDecision."""

    def __init__(self, agent_id: int | str, model: ModelConfig, template: str, client: LLMClient,
                 sleep: Callable[[float], None] = time.sleep, backoff: Sequence[float] = BACKOFF_S):
        profile(agent_id)                     # unknown agent id fails at construction
        self.agent_id = agent_id
        self.model = model
        self.template = template
        self._client = client
        self._sleep = sleep
        self._backoff = tuple(backoff)

    def __call__(self, snapshot: "Snapshot", portfolio: Portfolio, context: Context) -> AIDecision:
        return self.decide(snapshot, portfolio, context)

    def decide(self, snapshot: "Snapshot", portfolio: Portfolio, context: Context) -> AIDecision:
        base = build_prompt(self.template, self.agent_id, snapshot, portfolio, context)
        universe = tuple(stock_fields(snapshot))
        attempts: list[AttemptRecord] = []
        prompt = base
        for _ in range(2):                    # the call, and one retry on invalid output
            try:
                resp = self._send(prompt, attempts)
            except _Stop as stop:
                return self._hold(stop.status, attempts)
            wrong_host = True                 # wrong provider or model: the model made no mistake
            try:
                if resp.provider_served not in self.model.provider_names:
                    raise OutputError(f"served by {resp.provider_served!r}, "
                                      f"pinned to {' / '.join(map(repr, self.model.provider_names))}")
                if not model_matches(self.model.model_id, resp.model_id):
                    raise OutputError(f"wrong model: requested {self.model.model_id!r}, "
                                      f"reported {resp.model_id!r}")
                wrong_host = False
                if resp.finish_reason == "length":
                    raise OutputError(f"reply cut off at the {self.model.max_tokens}-token output limit; "
                                      f"reply more briefly")
                parsed = parse_and_validate(resp.text, universe, snapshot.asset_class)
            except OutputError as e:
                attempts.append(self._attempt(attempts, "invalid", prompt, resp, error=str(e)))
                prompt = base if wrong_host else base + RETRY_NOTE.format(error=e)
                continue
            attempts.append(self._attempt(attempts, "ok", prompt, resp, parsed=parsed))
            record = DecisionRecord("ok", tuple(attempts))
            return AIDecision(None if parsed.targets is None else dict(parsed.targets),
                              _one_line(parsed.reasoning), record)
        return self._hold("invalid", attempts)

    # -- helpers --

    def _send(self, prompt: str, attempts: list[AttemptRecord]) -> LLMResponse:
        for wait in (0, *self._backoff):
            if wait:
                self._sleep(wait)
            try:
                return self._client.complete(prompt, model=self.model)
            except LLMCapped as e:
                attempts.append(self._attempt(attempts, "capped", prompt, None, error=f"capped: {e}"))
                raise _Stop("capped") from None
            except LLMUnavailable as e:
                attempts.append(self._attempt(attempts, "unavailable", prompt, None,
                                              error=f"unavailable: {e}"))
        raise _Stop("unavailable")

    def _attempt(self, attempts, status, prompt, resp: LLMResponse | None, parsed=None, error=None):
        common = dict(attempt=len(attempts) + 1, status=status, model_id=self.model.model_id,
                      reasoning_setting=self.model.reasoning, temperature=self.model.temperature,
                      prompt_hash=sha256(prompt), parsed=parsed, error=error)
        if resp is None:
            return AttemptRecord(**common)
        common["model_id"] = resp.model_id            # the model id the provider reports
        return AttemptRecord(**common, provider_served=resp.provider_served, raw_response=resp.text,
                             tokens_in=resp.tokens_in, tokens_out=resp.tokens_out,
                             cost_usd=resp.cost_usd, latency_ms=resp.latency_ms,
                             reasoning_tokens=resp.reasoning_tokens)

    def _hold(self, status: str, attempts: list[AttemptRecord]) -> AIDecision:
        last = attempts[-1].error or status
        return AIDecision(None, _one_line(f"held ({status}): {last}"), DecisionRecord(status, tuple(attempts)))
