"""Agent 2 · Equal-weight hold (protocol §5): 1/22 in each stock, rebalanced monthly.

Targets 1/N in every stock in the snapshot except SPY (N = 22 for the frozen
universe) on the first session of each month (day 1 of the run counts, see
build_context), or on any decision where it holds none of those stocks yet, so
a missed first decision is caught the next day. Otherwise it holds.

The stock list comes from snapshot.symbols, not the universe file: the agent
sees only the snapshot.
"""

from __future__ import annotations

from strategies.base import Context, Decision, Portfolio

AGENT_ID = 2
NAME = "Equal-weight hold"
EXCLUDE = frozenset({"SPY"})


def decide(snapshot, portfolio: Portfolio, context: Context) -> Decision:
    stocks = sorted(s for s in snapshot.symbols if s not in EXCLUDE)
    if not stocks:
        return Decision.hold("No stocks in the snapshot")
    if context.is_first_session_of_month:
        why = "First session of the run" if context.is_first_day_of_run else "First session of the month"
    elif not any(portfolio.holds(s) for s in stocks):
        why = "Not invested yet (first decision missed)"
    else:
        return Decision.hold("Holding until the first session of next month")
    w = 1.0 / len(stocks)
    return Decision({s: w for s in stocks}, f"{why}: 1/{len(stocks)} in each stock")
