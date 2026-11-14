# Decisions

Design decisions for AlphaLab, newest at the bottom. Each one says what was decided, why, and what would make us revisit it. Once the official run starts (Mon 11 Jan 2027), a decision can only change by starting a new experiment.

---

## D1 · How the agents trade when Alpaca gives 3 paper accounts

**Decided:** Sat 14 Nov 2026 · **Status:** accepted

**Context.** Each Alpaca login gets three paper accounts. Alpaca's paper trading also skips borrow fees, slippage and dividends, so its fills are kinder than real trading.

**Decision.** All 15 equity agents (originally 14; see D9) trade through our own simulated ledger, priced from Alpaca market data. Three agents are also mirrored on real Alpaca paper accounts as a cross-check:

| Alpaca paper account | Mirrors agent |
|---|---|
| 1 | Agent 1 · Benchmark (SPY) |
| 2 | Agent 8 · Autonomous Claude |
| 3 | Agent 13 · Adaptive blender (agent 12 before D9) |

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

---

## D6 · Decision frequency

**Decided:** Sat 14 Nov 2026 · **Status:** accepted

**Decision.** Every agent decides **once per trading day, after the close**, and fills at the next day's open. No intraday trading.

**Why.** Intraday trading would need the engine to work perfectly during the school day (14:30–21:00 UK), would multiply AI API costs about 26 times, needs better data than the free IEX feed, and makes the no-future-data rule much harder to prove. Daily decisions keep the experiment cheap, clean and reliable.

---

## D7 · Crypto side experiment

**Decided:** Sat 14 Nov 2026 · **Status:** accepted; details fixed at Gate 1

**Decision.** Run a small crypto side experiment alongside the main one, on the same engine and dashboard, but **reported separately**, never ranked against the equity agents:

| Agent | What it does |
|---|---|
| C0 · Bitcoin benchmark | Buys BTC on day 1 and holds (the control) |
| C1 · AI crypto trader (Claude) | Trades a small set of large coins, long only, aiming for the highest return it can |

**Why separate.** Crypto trades 24/7, can't be shorted or traded on margin at Alpaca, and is far more volatile, so its numbers aren't comparable with the equity agents. **Why a benchmark.** Without C0 there's no way to tell whether C1 added anything beyond "crypto went up" or "crypto went down".

**In the paper:** a short side note comparing C1 with C0.

---

## D8 · How we reach the AI models

**Decided:** Sat 14 Nov 2026 · **Status:** accepted

**Decision.** All models are called through **OpenRouter**: one account, one key, one prepaid balance and one spend limit, owned by a parent. Each model is **pinned to its official provider** (Anthropic, OpenAI, Google, Moonshot) with fallbacks to other hosts turned off, so every call goes to the real model from its maker.

**Why.** One sign-up instead of four, one bill and one cap. The 5.5% credit fee is under a dollar a month at our spend. If OpenRouter is down, every AI agent holds that day, which affects all models equally.

**Revisit if:** OpenRouter can't pin a model to its official provider, or its outages cost more than a few days during the shakedown.

---

## D9 · Model lineup

**Decided:** Sat 14 Nov 2026 · **Status:** accepted; exact model versions frozen at Gate 1

**Decision.** The autonomous model comparison uses **five** models: **Claude** (Anthropic), **GPT** (OpenAI), **Gemini** (Google), **DeepSeek** and **Grok** (xAI). Kimi (Moonshot) is dropped. This adds one agent, so the roster becomes **15 equity agents**, renumbered:

| # | Agent |
|---|---|
| 1–6 | Rule-based (unchanged) |
| 7 | Constrained AI (Claude) |
| 8–12 | Autonomous Claude, GPT, Gemini, DeepSeek, Grok |
| 13 | Adaptive blender (Claude) |
| 14 | AI Cautious (Claude) |
| 15 | AI Aggressive (Claude) |

**Why.** The best-known live test of AI traders, nof1's Alpha Arena (Oct–Nov 2025), used Claude, GPT, Gemini, Grok, DeepSeek and Qwen on crypto. Matching its lineup lets the paper compare results directly, including whether DeepSeek's strong showing there holds up under controls, benchmarks, identical costs and a 12-week run. Five models is the limit: each extra agent adds cost and makes a lucky "winner" more likely.

**Note:** the smoke test on Sat 14 Nov confirmed one OpenRouter key reaches Claude, GPT, Gemini and Kimi. DeepSeek and Grok are checked at Gate 1.
