"""Agent 1 · Benchmark (protocol §5): 100% SPY on day 1, held for the whole run.

It also buys on any later decision where the portfolio holds no SPY yet, so a
missed or failed day-1 decision is caught the next day. Otherwise it holds.
"""

from __future__ import annotations

from strategies.base import Context, Decision, Portfolio

AGENT_ID = 1
NAME = "Benchmark (SPY)"
SYMBOL = "SPY"


def decide(snapshot, portfolio: Portfolio, context: Context) -> Decision:
    if context.is_first_day_of_run:
        return Decision({SYMBOL: 1.0}, "Day 1 of the run: 100% SPY")
    if not portfolio.holds(SYMBOL):
        return Decision({SYMBOL: 1.0}, "No SPY held yet (day 1 missed): 100% SPY")
    return Decision.hold("Holding SPY")
