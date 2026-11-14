# Decisions

Design decisions for AlphaLab, newest at the bottom. Each one says what was decided, why, and what would make us revisit it. Once the official run starts (Mon 11 Jan 2027), a decision can only change by starting a new experiment.

---

## D1 · How 14 agents trade when Alpaca gives 3 paper accounts

**Decided:** Sat 14 Nov 2026 · **Status:** accepted

**Context.** Each Alpaca login gets three paper accounts. Alpaca's paper trading also skips borrow fees, slippage and dividends, so its fills are kinder than real trading.

**Decision.** All 14 agents trade through our own simulated ledger, priced from Alpaca market data. Three agents are also mirrored on real Alpaca paper accounts as a cross-check:

| Alpaca paper account | Mirrors agent |
|---|---|
| 1 | Agent 1 · Benchmark (SPY) |
| 2 | Agent 8 · Autonomous Claude |
| 3 | Agent 12 · Adaptive blender |

**Why.** One ledger means every agent gets an identical cost, slippage and borrow model, which makes the comparison fair. It is also more realistic than Alpaca's paper fills. The ledger's maths becomes part of the project rather than a black box.

**Results come from the ledger.** The mirrors are only for reconciliation. A gap between an agent's ledger and its mirror is logged and explained, never "corrected" in the ledger.

**Revisit if:** reconciliation shows the ledger is systematically wrong in a way we can't explain.

---

## D2 · Where the engine runs over the holidays

**Decided:** deferred to Gate 2 (first day back in January) · **Status:** open, by design

**Context.** The Pi was offline for 12 days over half term and again on 11 Nov (hidden SSID). Signal in the room is weak away from the window. A January–April run crosses Easter.

**Rule for Gate 2.** Measure the engine's uptime over the Christmas shakedown (Mon 14 Dec – Sun 10 Jan):

- **99% or more of scheduled runs completed:** the engine stays on the Pi.
- **Below 99%:** the engine moves to a small cloud server. The Pi stays as the command centre, dev box and nightly replica.

The rule is fixed now, before the shakedown data exists, so the decision can't be bent to fit what we'd like.

---

## D3 · Start date and length

**Decided:** Sat 14 Nov 2026 · **Status:** accepted

**Decision.** The official run starts **Mon 11 Jan 2027** and runs **12 weeks, to Fri 2 Apr 2027** (about 60 trading days).

**Why.** That's the first full trading week after returning in January, after the shakedown has proven the system. Twelve weeks is long enough to see behaviour across different market days. It is **not** long enough to prove one strategy beats another, so the paper leads with behaviour, reasoning and effect sizes with confidence intervals, not "AI wins".

**Revisit if:** Gate 2 fails (the system isn't stable). Then the start moves back a week at a time, and the length stays 12 weeks.

---

## D4 · The universe

**Decided:** Sat 14 Nov 2026 · **Status:** accepted; the exact list is fixed at Gate 1

**Decision.** About 22 large, liquid US stocks plus SPY. They are chosen by a fixed rule, not by hand: the **top 2 holdings by weight of each of the 11 Select Sector SPDR ETFs** on the freeze date (rule details in the protocol, §2).

**Why.** A rule avoids selection bias ("I picked stocks I thought would do well"). Two per sector gives enough spread for momentum, mean reversion and long–short to mean something. Every name is liquid and normally easy to borrow. 22 names keeps the daily AI snapshot small and cheap.

---

## D5 · What the AI agents see

**Decided:** Sat 14 Nov 2026 · **Status:** accepted

**Decision.** AI agents get **prices and indicators only**: the same data the rule-based agents use, plus their own portfolio. No news, no fundamentals.

**Why.** Fairest comparison: any difference between AI and rules comes from how they *use* the same information, not from extra information. It's also cheapest, and avoids news timestamps, which are the easiest way to leak future data.

**Possible later experiment:** a second run that adds news, as an "information tier" study. Not in this run.
