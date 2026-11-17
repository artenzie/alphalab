"""Agent C0 · Bitcoin benchmark (protocol §13): 100% BTC on day 1, held for the whole run.

Like agent 1, it also buys on any later decision where the portfolio holds no
BTC yet, so a missed or failed day-1 decision is caught the next day.
Otherwise it holds. Decides every calendar day after the 00:00 UTC bar (§13).
"""

from __future__ import annotations

from strategies.base import Context, Decision, Portfolio

AGENT_ID = "C0"
NAME = "Bitcoin benchmark"
SYMBOL = "BTC/USD"


def decide(snapshot, portfolio: Portfolio, context: Context) -> Decision:
    if context.is_first_day_of_run:
        return Decision({SYMBOL: 1.0}, "Day 1 of the run: 100% BTC")
    if not portfolio.holds(SYMBOL):
        return Decision({SYMBOL: 1.0}, "No BTC held yet (day 1 missed): 100% BTC")
    return Decision.hold("Holding BTC")
