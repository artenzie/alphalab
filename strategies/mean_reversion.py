"""Agent 4 · Mean reversion (protocol §5, v1.4): daily, on the 20-day z-score.

For each stock (never SPY), from its current position and z_20:
  long,  z >= 0      exit
  short, z <= 0      exit
  flat,  z <= -1.5   long entry signal  (+10% of current portfolio value)
  flat,  z >= +1.5   short entry signal (-10%)
A stock that exits today counts as flat for entries, so a long at z >= +1.5
can flip to a short if a short slot is free.

Capacity: at most 10 open positions, at most 3 of them short. Exits free their
slots first. Entry signals then fill the free slots by the largest |z| first,
ties by symbol (A first); a short signal with no short slot left is skipped and
the next signal is tried. Skipped signals are named in the reason.

No resizing: an open position that isn't exiting keeps the weight it has
drifted to, shares x snapshot close / portfolio value, so the ledger sees no
change for it. A held stock with no z_20 stays open. Orders come only from
entries and exits; if there are none it holds. Day 1 of the run always gives a
decision, even if that is all cash. Risk limits (§6) are not applied here.
"""

from __future__ import annotations

from strategies.base import Context, Decision, Portfolio, stock_fields

AGENT_ID = 4
NAME = "Mean reversion"
ENTRY_Z = 1.5
WEIGHT = 0.10
MAX_POSITIONS = 10
MAX_SHORTS = 3


def _drifted_weight(portfolio: Portfolio, symbol: str, close, side: int) -> float:
    """Current weight of an open position; the entry weight if it can't be priced."""
    if close is None or portfolio.value <= 0:
        return side * WEIGHT
    return portfolio.shares(symbol) * close / portfolio.value


def decide(snapshot, portfolio: Portfolio, context: Context) -> Decision:
    fields = stock_fields(snapshot)
    changes: list[str] = []

    # 1. exits
    staying: dict[str, int] = {}                       # symbol -> side (+1 long, -1 short)
    for s in sorted(fields):
        pos = portfolio.shares(s)
        if pos == 0:
            continue
        side = 1 if pos > 0 else -1
        z = fields[s].get("z_20")
        if z is not None and ((side > 0 and z >= 0) or (side < 0 and z <= 0)):
            changes.append(f"exit {'long' if side > 0 else 'short'} {s} (z {z:+.2f})")
        else:
            staying[s] = side

    # 2. entries into free slots, most extreme |z| first, ties A->Z
    signals = []
    for s, f in fields.items():
        z = f.get("z_20")
        if s in staying or z is None:
            continue
        if z <= -ENTRY_Z:
            signals.append((s, 1, z))
        elif z >= ENTRY_Z:
            signals.append((s, -1, z))
    signals.sort(key=lambda t: (-abs(t[2]), t[0]))
    slots = max(0, MAX_POSITIONS - len(staying))
    short_slots = max(0, MAX_SHORTS - sum(1 for v in staying.values() if v < 0))
    entries: dict[str, int] = {}
    skipped: list[str] = []
    for s, side, z in signals:
        if slots == 0 or (side < 0 and short_slots == 0):
            skipped.append(f"{s} ({z:+.2f})")
            continue
        entries[s] = side
        slots -= 1
        short_slots -= side < 0
        changes.append(f"{'long' if side > 0 else 'short'} {s} (z {z:+.2f})")

    # 3. anything else held (SPY, a stock gone from the snapshot) is outside the rule: close it
    changes += [f"close {s} (not a rule stock)"
                for s in sorted(portfolio.positions) if s not in fields and portfolio.holds(s)]

    weights = {s: _drifted_weight(portfolio, s, fields[s].get("last_close"), side)
               for s, side in staying.items()}
    weights |= {s: side * WEIGHT for s, side in entries.items()}
    note = f"; skipped, no slot: {', '.join(skipped)}" if skipped else ""
    if changes:
        return Decision(weights, "; ".join(changes) + note)
    if context.is_first_day_of_run:
        return Decision(weights, f"First session of the run: {len(weights)} position(s) open, no entries{note}")
    return Decision.hold(f"No entries or exits; {len(weights)} position(s) open{note}")
