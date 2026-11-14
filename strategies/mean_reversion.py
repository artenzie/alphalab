"""Agent 4 · Mean reversion (protocol §5, v1.3): daily, on the 20-day z-score.

For each stock (never SPY), from its current position and z_20:
  flat,  z <= -1.5   enter long  +10%
  flat,  z >= +1.5   enter short -10%
  long,  z >= 0      exit (and if z >= +1.5, go short -10% in the same decision)
  short, z <= 0      exit (and if z <= -1.5, go long +10%)
  otherwise          an open position keeps its 10% target; flat stays flat
A stock with no z_20 keeps an open position and isn't entered.

If nothing enters or exits it holds, so it doesn't rebalance drift every day.
Day 1 of the run always gives a full decision, even if that is all cash.
Risk limits (§6) are not applied here; the risk checks clip and log.
"""

from __future__ import annotations

from strategies.base import Context, Decision, Portfolio, stock_fields

AGENT_ID = 4
NAME = "Mean reversion"
ENTRY_Z = 1.5
WEIGHT = 0.10


def decide(snapshot, portfolio: Portfolio, context: Context) -> Decision:
    fields = stock_fields(snapshot)
    weights: dict[str, float] = {}
    changes: list[str] = []
    for s in sorted(fields):
        z = fields[s].get("z_20")
        pos = portfolio.shares(s)
        side = (pos > 0) - (pos < 0)
        if z is None:
            if side:
                weights[s] = side * WEIGHT
            continue
        if side > 0 and z >= 0:
            changes.append(f"exit long {s} (z {z:+.2f})")
            side = 0
        elif side < 0 and z <= 0:
            changes.append(f"exit short {s} (z {z:+.2f})")
            side = 0
        elif side == 0 and z > -ENTRY_Z and z < ENTRY_Z:
            continue
        if side == 0:
            if z <= -ENTRY_Z:
                side = 1
                changes.append(f"long {s} (z {z:+.2f})")
            elif z >= ENTRY_Z:
                side = -1
                changes.append(f"short {s} (z {z:+.2f})")
        if side:
            weights[s] = side * WEIGHT
    # anything else held (SPY, a stock gone from the snapshot) is not part of the rule: close it
    stray = sorted(s for s in portfolio.positions if s not in fields and portfolio.holds(s))
    changes += [f"close {s} (not a rule stock)" for s in stray]
    if changes:
        return Decision(weights, "; ".join(changes))
    if context.is_first_day_of_run:
        n = len(weights)
        return Decision(weights, f"First session of the run: {n} position(s) open, no signals")
    return Decision.hold(f"No entries or exits; {len(weights)} position(s) open")
