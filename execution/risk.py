"""Risk checks (protocol §6, v1.4; crypto limits §13): pure functions.

    weights, events = apply_risk(agent_id, decision, etb_flags, current_weights)
    weights, events = margin_check(equity, gross_exposure, positions)

No database, network, file or environment access. Easy-to-borrow flags and
current weights come in as arguments: the runner fetches the flags from Alpaca
at decision time and computes the weights from the agent's own holdings.

apply_risk runs after every non-hold decision, before any order, in this order:

  1. shorts_not_allowed  a short for an agent without shorts is rejected (-> 0)
  2. easy_to_borrow      a short that is new, or larger than the current short,
                         in a stock not flagged easy-to-borrow is rejected: a new
                         short -> 0, a larger one -> kept at its current size.
                         An existing short is never closed by this rule (§6:
                         risk checks never add trades the agent didn't ask for)
  3. max_per_stock       a position above the per-stock cap is cut to the cap
  4. max_total_short     all shorts are scaled pro rata to the total-short cap
  5. max_gross           all positions are scaled pro rata to the gross cap

Every step only sets a weight to zero, cuts it, or multiplies it by a factor in
[0, 1], so no output weight is ever larger in size than the agent asked for, and
no sign is flipped. A hold decision (weights None) passes through untouched:
drift is only checked on decision days. margin_check runs every day.

apply_eligibility (agent 7, §5 v1.15) runs after apply_risk, on every day:
a held stock that is not in today's eligible list is sold (weight 0), on a
hold, a rebalance or an invalid day alike, one 'forced_reduction' event with
rule 'eligibility' per stock. Kept positions keep their current weight, so
they get no trade. Like the margin cut, it is a forced sale the agent didn't
ask for (§6).

Each clip or rejection is one event per symbol, as a dict whose keys are the
risk_events columns (event_type, rule, symbol, requested_weight,
applied_weight, detail); detail is a JSON string. The runner adds agent_id,
decision_id and occurred_at.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

# Limits are compared with this tolerance, so float noise (5 x 0.30 summing to
# 1.5000000000000002) is not a breach, while a real 1% overshoot is.
TOL = 1e-9

MARGIN_TRIGGER = 0.30   # equity below 30% of gross exposure ...
MARGIN_TARGET = 0.40    # ... cut pro rata until equity is 40% of gross


@dataclass(frozen=True)
class Profile:
    name: str
    max_per_stock: float
    shorts: bool
    max_gross: float
    max_total_short: float


_MARKET_SPY = Profile("market", 1.0, False, 1.0, 0.0)          # agent 1: 100% SPY
_MARKET_EW = Profile("market", 1 / 22, False, 1.0, 0.0)        # agent 2: 1/22 each
_MEDIUM_LONG = Profile("medium", 0.20, False, 1.0, 0.0)
_MEDIUM_SHORT = Profile("medium", 0.20, True, 1.0, 0.30)
_MEDIUM_SHORT_50 = Profile("medium", 0.20, True, 1.0, 0.50)    # agents 6 and 13
_LOW = Profile("low", 0.10, False, 1.0, 0.0)
_HIGH = Profile("high", 0.30, True, 1.5, 0.50)
_C1 = Profile("crypto", 0.50, False, 1.0, 0.0)                 # §13: long only, 50% per coin
_C0 = Profile("crypto", 1.0, False, 1.0, 0.0)                  # §13: 100% BTC

PROFILES: Mapping[str, Profile] = MappingProxyType({
    "1": _MARKET_SPY, "2": _MARKET_EW,
    "3": _MEDIUM_LONG, "5": _MEDIUM_LONG, "7": _MEDIUM_LONG,
    "4": _MEDIUM_SHORT, "8": _MEDIUM_SHORT, "9": _MEDIUM_SHORT, "10": _MEDIUM_SHORT,
    "11": _MEDIUM_SHORT, "12": _MEDIUM_SHORT,
    "6": _MEDIUM_SHORT_50, "13": _MEDIUM_SHORT_50,
    "14": _LOW,
    "15": _HIGH,
    "C0": _C0, "C1": _C1,
})


def profile(agent_id: int | str) -> Profile:
    key = str(agent_id)
    try:
        return PROFILES[key]
    except KeyError:
        raise KeyError(f"no risk profile for agent {agent_id!r}; known: {sorted(PROFILES)}") from None


def _event(event_type: str, rule: str, symbol: str | None, requested: float | None,
           applied: float | None, **detail) -> dict:
    return {"event_type": event_type, "rule": rule, "symbol": symbol,
            "requested_weight": requested, "applied_weight": applied,
            "detail": json.dumps(detail, sort_keys=True)}


def _check_weights(weights: Mapping[str, float], what: str) -> dict[str, float]:
    out = {}
    for s, w in weights.items():
        if isinstance(w, bool) or not isinstance(w, (int, float)) or not math.isfinite(w):
            raise ValueError(f"{what} for {s} must be a finite number, got {w!r}")
        out[s] = float(w)
    return out


def apply_risk(agent_id: int | str, decision, etb_flags: Mapping[str, bool],
               current_weights: Mapping[str, float]) -> tuple[dict[str, float] | None, list[dict]]:
    """Clip a decision's target weights to the agent's §6 limits.

    decision         strategies.base.Decision (weights None = hold)
    etb_flags        symbol -> easy-to-borrow at decision time; a missing symbol
                     counts as not easy-to-borrow
    current_weights  symbol -> the agent's current weight (negative = short);
                     a missing symbol counts as 0

    Returns (weights, events). weights is None for a hold, otherwise a new dict
    with the same symbols as the decision.
    """
    prof = profile(agent_id)
    if decision.weights is None:
        return None, []
    requested = _check_weights(decision.weights, "target weight")
    current = _check_weights(current_weights, "current weight")
    w = dict(requested)
    events: list[dict] = []

    # 1. shorts not allowed for this agent
    if not prof.shorts:
        for s in sorted(w):
            if w[s] < 0:
                events.append(_event("reject", "shorts_not_allowed", s, w[s], 0.0, profile=prof.name))
                w[s] = 0.0

    # 2. easy-to-borrow: blocks opening or increasing a short, never closes one
    for s in sorted(w):
        if w[s] < 0 and not etb_flags.get(s, False):
            cur = current.get(s, 0.0)
            held_short = min(cur, 0.0)                  # current short size (<= 0)
            if -w[s] > -held_short + TOL:
                applied = held_short                    # 0 for a new short
                events.append(_event("reject", "easy_to_borrow", s, w[s], applied,
                                     etb=bool(etb_flags.get(s, False)), in_flags=s in etb_flags,
                                     current_weight=cur))
                w[s] = applied

    # 3. per-stock cap
    cap = prof.max_per_stock
    for s in sorted(w):
        if abs(w[s]) > cap + TOL:
            applied = math.copysign(cap, w[s])
            events.append(_event("clip", "max_per_stock", s, w[s], applied, limit=cap))
            w[s] = applied

    # 4. total-short cap: scale all shorts pro rata
    total_short = -sum(v for v in w.values() if v < 0)
    if total_short > prof.max_total_short + TOL:
        factor = prof.max_total_short / total_short
        for s in sorted(w):
            if w[s] < 0:
                applied = w[s] * factor
                events.append(_event("clip", "max_total_short", s, w[s], applied, limit=prof.max_total_short,
                                     total_short=total_short, factor=factor))
                w[s] = applied

    # 5. gross cap: scale everything pro rata
    gross = sum(abs(v) for v in w.values())
    if gross > prof.max_gross + TOL:
        factor = prof.max_gross / gross
        for s in sorted(w):
            if w[s] != 0:
                applied = w[s] * factor
                events.append(_event("clip", "max_gross", s, w[s], applied, limit=prof.max_gross,
                                     gross=gross, factor=factor))
                w[s] = applied

    for s, v in w.items():  # the invariant every rule above keeps
        r = requested[s]
        assert abs(v) <= abs(r) and (v == 0 or (v > 0) == (r > 0)), (s, r, v)
    return w, events


def apply_eligibility(weights: Mapping[str, float] | None, current_weights: Mapping[str, float],
                      eligible, decision: str) -> tuple[dict[str, float] | None, list[dict]]:
    """Agent 7's eligibility sale (§5, v1.15), after apply_risk.

    weights          the risk-checked targets, or None (a hold, or an invalid day's hold)
    current_weights  symbol -> the agent's current weight
    eligible         today's eligible stocks (agent 3's top 5 by ret_63)
    decision         'hold', 'rebalance', or the status of a failed day ('invalid',
                     'unavailable', 'capped'), recorded in each event

    A held stock (current weight != 0) or a non-zero target outside eligible
    goes to 0, one event each. With nothing to sell, the weights come back
    unchanged (None stays a hold). On a hold with something to sell, the
    targets are the current weights with the sold stocks at 0.
    """
    if decision not in ("hold", "rebalance", "invalid", "unavailable", "capped"):
        raise ValueError(f"unknown decision kind {decision!r}")
    ok = set(eligible)
    current = _check_weights(current_weights, "current weight")
    w = None if weights is None else _check_weights(weights, "target weight")
    held_out = {s for s, v in current.items() if v != 0 and s not in ok}
    asked_out = set() if w is None else {s for s, v in w.items() if v != 0 and s not in ok}
    out = sorted(held_out | asked_out)
    if not out:
        return (None if w is None else w), []
    if w is None:
        w = {s: v for s, v in current.items() if v != 0}
    events = []
    for s in out:
        events.append(_event("forced_reduction", "eligibility", s, w.get(s, 0.0), 0.0, decision=decision,
                             eligible=list(eligible), current_weight=current.get(s, 0.0)))
        w[s] = 0.0
    return w, events


def margin_check(equity: float, gross_exposure: float,
                 positions: Mapping[str, float]) -> tuple[dict[str, float] | None, list[dict]]:
    """Forced reduction on a margin breach (§6), run every day.

    equity          the agent's portfolio value in dollars
    gross_exposure  sum of |position value| in dollars (must match positions)
    positions       symbol -> signed market value in dollars (negative = short)

    If equity < 30% of gross exposure, every position is scaled by the same
    factor so that equity is 40% of the new gross. Returns (target weights as a
    fraction of equity, events), or (None, []) when there is no breach. With
    equity <= 0 everything is closed (all weights 0).
    """
    pos = _check_weights(positions, "position value")
    for name, x in (("equity", equity), ("gross_exposure", gross_exposure)):
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
            raise ValueError(f"{name} must be a finite number, got {x!r}")
    gross = sum(abs(v) for v in pos.values())
    if not math.isclose(gross, gross_exposure, rel_tol=1e-9, abs_tol=1e-6):
        raise ValueError(f"gross_exposure {gross_exposure} != sum of |positions| {gross}")
    # compared as a ratio with a tolerance: 0.30 * 100 is 30.000000000000004 in floats
    if gross == 0 or equity / gross >= MARGIN_TRIGGER - TOL:
        return None, []

    if equity <= 0:
        out = {s: 0.0 for s in pos}
        events = [_event("forced_reduction", "margin", s, None, 0.0, equity=equity, gross_exposure=gross,
                         position_value=pos[s], action="close_all") for s in sorted(pos) if pos[s] != 0]
        return out, events

    factor = equity / (MARGIN_TARGET * gross)   # new gross = equity / 0.40
    out, events = {}, []
    for s in sorted(pos):
        before, after = pos[s] / equity, pos[s] * factor / equity
        out[s] = after
        if pos[s] != 0:
            events.append(_event("forced_reduction", "margin", s, before, after, equity=equity,
                                 gross_exposure=gross, equity_to_gross=equity / gross,
                                 trigger=MARGIN_TRIGGER, target=MARGIN_TARGET, factor=factor))
    return out, events
