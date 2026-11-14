"""Agent 3 · Momentum (protocol §5): top 5 stocks by 63-day return, 20% each, weekly.

Decides on the first session of each week (day 1 of the run counts), or on any
decision where it holds none of the stocks yet, so a missed first decision is
caught the next day. Otherwise it holds.

Ranking: ret_63 from the snapshot, highest first, ties by symbol (A first).
Stocks without ret_63 are left out; if fewer than 5 remain, each still gets
20% and the rest stays in cash. SPY is never traded.
"""

from __future__ import annotations

from strategies.base import Context, Decision, Portfolio, holds_any, rank, stock_fields

AGENT_ID = 3
NAME = "Momentum"
TOP_N = 5
WEIGHT = 0.20


def decide(snapshot, portfolio: Portfolio, context: Context) -> Decision:
    fields = stock_fields(snapshot)
    if context.is_first_session_of_week:
        why = "First session of the run" if context.is_first_day_of_run else "First session of the week"
    elif not holds_any(portfolio, fields):
        why = "Not invested yet (first decision missed)"
    else:
        return Decision.hold("Holding until the first session of next week")
    top = rank(fields, "ret_63")[:TOP_N]
    if not top:
        return Decision.hold(f"{why}: no stock has a 63-day return")
    return Decision({s: WEIGHT for s in top}, f"{why}: top {len(top)} by 63-day return: {', '.join(top)}")
