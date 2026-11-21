"""The one interface every agent uses (protocol §5), rule-based now, AI later.

    decision = agent(snapshot, portfolio, context)

  snapshot   data.snapshot.Snapshot: the only market data an agent sees. It
             holds no database connection (CLAUDE.md rule 1).
  portfolio  Portfolio: the agent's own holdings, read-only.
  context    Context: calendar facts computed by the runner with
             build_context(), never by the agent.

An agent returns a Decision: target weights per symbol (fraction of current
portfolio value, negative = short), or weights=None to hold with no change,
plus a one-line reason. Non-hold weights are the complete target portfolio
(protocol §7 rule 1, v1.8): a held symbol left out is sold to 0. Risk limits (§6) are applied afterwards by the risk
checks, not here. Agents get no database, file, network or environment access.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from typing import TYPE_CHECKING, Callable, Mapping, Sequence

if TYPE_CHECKING:  # type hints only: agents never import the data layer at runtime
    from data.snapshot import Snapshot


class DecisionError(ValueError):
    """An agent produced a malformed decision."""


def _frozen_weights(weights: Mapping[str, float]) -> Mapping[str, float]:
    out: dict[str, float] = {}
    for sym, w in weights.items():
        if not isinstance(sym, str) or not sym:
            raise DecisionError(f"bad symbol {sym!r}")
        if isinstance(w, bool) or not isinstance(w, (int, float)) or not math.isfinite(w):
            raise DecisionError(f"weight for {sym} must be a finite number, got {w!r}")
        out[sym] = float(w)
    return MappingProxyType(out)


@dataclass(frozen=True)
class Decision:
    """Target weights per symbol, or None = hold (no change), and a one-line reason."""
    weights: Mapping[str, float] | None
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise DecisionError("a decision needs a reason")
        if "\n" in self.reason or "\r" in self.reason:
            raise DecisionError("the reason must be one line")
        if self.weights is not None:
            object.__setattr__(self, "weights", _frozen_weights(self.weights))

    @property
    def is_hold(self) -> bool:
        return self.weights is None

    @classmethod
    def hold(cls, reason: str) -> "Decision":
        return cls(None, reason)


@dataclass(frozen=True)
class Portfolio:
    """An agent's own holdings: cash, shares per symbol (negative = short), total value."""
    cash: float
    positions: Mapping[str, float] = field(default_factory=dict)
    value: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "positions", MappingProxyType(dict(self.positions)))

    def shares(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0)

    def holds(self, symbol: str) -> bool:
        return self.shares(symbol) != 0


@dataclass(frozen=True)
class PastDecision:
    """One of an AI agent's own earlier decisions, as shown back to it (protocol §4)."""
    decision_date: date
    status: str                                  # 'ok', 'invalid', 'unavailable', 'capped'
    targets: Mapping[str, float] | None          # None = hold
    reasons: Mapping[str, str] = field(default_factory=dict)
    reasoning: str = ""

    def __post_init__(self) -> None:
        if self.targets is not None:
            object.__setattr__(self, "targets", MappingProxyType(dict(self.targets)))
        object.__setattr__(self, "reasons", MappingProxyType(dict(self.reasons)))


@dataclass(frozen=True)
class AgentMemory:
    """The §4 AI inputs a Portfolio doesn't carry, read from the log and the label by the runner.

    end_date is the run's last decision day (the experiment label, v1.15) and
    trading_days_left the run's trading days after the decision date: NYSE
    sessions for equity, calendar days for crypto, 0 on the last day.
    """
    starting_value: float
    avg_cost: Mapping[str, float] = field(default_factory=dict)
    last_decisions: tuple[PastDecision, ...] = ()
    end_date: date | None = None
    trading_days_left: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "avg_cost", MappingProxyType(dict(self.avg_cost)))
        object.__setattr__(self, "last_decisions", tuple(self.last_decisions))


@dataclass(frozen=True)
class Context:
    """Calendar facts for the decision, computed by the runner (build_context).

    memory is set by the runner for AI agents only (dataclasses.replace);
    rule-based agents ignore it.
    """
    decision_date: date
    is_first_day_of_run: bool
    is_first_session_of_week: bool
    is_first_session_of_month: bool
    memory: AgentMemory | None = None


def build_context(decision_date: date, run_sessions: Sequence[date]) -> Context:
    """Context for decision_date from the run's trading sessions up to and including it.

    run_sessions comes from the market calendar (data.calendar), first session
    of the run first. On the first day of the run every periodic flag is True,
    so weekly and monthly agents make their first decision then (protocol §5, v1.2).
    """
    sessions = sorted(run_sessions)
    if not sessions or sessions[-1] != decision_date:
        raise ValueError(f"run_sessions must end with the decision date {decision_date}")
    if len(set(sessions)) != len(sessions):
        raise ValueError("run_sessions has duplicate dates")
    if len(sessions) == 1:
        return Context(decision_date, True, True, True)
    prev = sessions[-2]
    return Context(
        decision_date,
        is_first_day_of_run=False,
        is_first_session_of_week=prev.isocalendar()[:2] != decision_date.isocalendar()[:2],
        is_first_session_of_month=(prev.year, prev.month) != (decision_date.year, decision_date.month),
    )


Agent = Callable[["Snapshot", Portfolio, Context], Decision]


# ---------------------------------------------------------------------------
# Helpers for the indicator agents (3-6)
# ---------------------------------------------------------------------------

BENCHMARK_SYMBOL = "SPY"


def stock_fields(snapshot: "Snapshot") -> dict[str, dict]:
    """Snapshot fields per stock: every snapshot symbol except SPY (protocol §5)."""
    symbols = snapshot.payload["symbols"]       # one parse; a fresh copy, safe to keep
    return {s: f for s, f in symbols.items() if s != BENCHMARK_SYMBOL}


def rank(fields: Mapping[str, Mapping], key: str) -> list[str]:
    """Stocks by fields[s][key], highest first, ties by symbol (A first); missing values left out."""
    have = [s for s, f in fields.items() if f.get(key) is not None]
    return sorted(have, key=lambda s: (-fields[s][key], s))


def ret63_top(snapshot: "Snapshot", n: int = 5) -> list[str]:
    """Today's top n stocks by ret_63, exactly as agent 3 ranks them: agent 7's eligible list (§5, v1.15)."""
    return rank(stock_fields(snapshot), "ret_63")[:n]


def holds_any(portfolio: Portfolio, symbols) -> bool:
    return any(portfolio.holds(s) for s in symbols)
