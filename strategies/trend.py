"""Agent 5 · Trend, MA crossover (protocol §5, v1.3): daily.

Equal weight in every stock (never SPY) whose ma_20 is above its ma_100; the
rest in cash. Stocks missing either average don't qualify. If the qualifying
set is exactly the set of stocks held (all long, nothing else held), it holds,
so it doesn't churn small rebalances every day. Day 1 of the run always gives
a full decision. An empty set means all cash ({}).
"""

from __future__ import annotations

from strategies.base import Context, Decision, Portfolio, stock_fields

AGENT_ID = 5
NAME = "Trend (MA crossover)"


def decide(snapshot, portfolio: Portfolio, context: Context) -> Decision:
    fields = stock_fields(snapshot)
    up = sorted(s for s, f in fields.items()
                if f.get("ma_20") is not None and f.get("ma_100") is not None and f["ma_20"] > f["ma_100"])
    held = {s for s in portfolio.positions if portfolio.holds(s)}
    all_long = all(portfolio.shares(s) > 0 for s in held)
    if not context.is_first_day_of_run and set(up) == held and all_long:
        return Decision.hold(f"Same {len(up)} stocks above their 100-day MA")
    why = "First session of the run" if context.is_first_day_of_run else "Set changed"
    if not up:
        return Decision({}, f"{why}: no stock has its 20-day MA above its 100-day MA; all cash")
    w = 1.0 / len(up)
    return Decision({s: w for s in up}, f"{why}: 1/{len(up)} in {', '.join(up)}")
