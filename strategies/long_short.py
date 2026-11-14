"""Agent 6 · Long-short momentum (protocol §5): long top 3, short bottom 3 by 63-day return.

+1/6 in each of the top 3 and -1/6 in each of the bottom 3 (net 0, gross 100%).
Decides on the first session of each week (day 1 of the run counts), or on any
decision where it holds none of the stocks yet. Otherwise it holds.

Ranking: ret_63, ties by symbol (A first) on both sides, so the bottom 3 are
the last 3 of that same order. Stocks without ret_63 are left out; with fewer
than 6 ranked stocks it holds. SPY is never traded.
"""

from __future__ import annotations

from strategies.base import Context, Decision, Portfolio, holds_any, rank, stock_fields

AGENT_ID = 6
NAME = "Long-short momentum"
SIDE_N = 3
WEIGHT = 1.0 / 6.0


def decide(snapshot, portfolio: Portfolio, context: Context) -> Decision:
    fields = stock_fields(snapshot)
    if context.is_first_session_of_week:
        why = "First session of the run" if context.is_first_day_of_run else "First session of the week"
    elif not holds_any(portfolio, fields):
        why = "Not invested yet (first decision missed)"
    else:
        return Decision.hold("Holding until the first session of next week")
    order = rank(fields, "ret_63")
    if len(order) < 2 * SIDE_N:
        return Decision.hold(f"{why}: only {len(order)} stocks have a 63-day return, need {2 * SIDE_N}")
    longs, shorts = order[:SIDE_N], order[-SIDE_N:]
    weights = {s: WEIGHT for s in longs} | {s: -WEIGHT for s in shorts}
    return Decision(weights, f"{why}: long {', '.join(longs)}; short {', '.join(shorts)}")
