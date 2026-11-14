# Experiment protocol

**Version:** v1.1, Sat 14 Nov 2026 (Gate 1 + crypto timings)
**Next:** final freeze as `protocol-v2.0` on Sun 10 Jan 2027, after the shakedown. Final freeze as `protocol-v2.0` on Sun 10 Jan 2027, after the shakedown.

All numbers were approved at the Gate 1 review on Sat 14 Nov 2026. The universe and model lineup were filled in from the Gate 1 scripts the same evening. Everything follows from decisions D1–D9 in `research/decisions.md`.

---

## 1 · Overview

Fifteen equity agents (6 rule-based, 9 AI) each start with the same virtual capital. Every trading day after the US close, one snapshot of market data is built. Each agent sees the snapshot (and its own portfolio), outputs target portfolio weights, passes risk checks, and is filled at the next day's open in a simulated ledger. Every step is logged in an append-only database.

| Item | Value |
|---|---|
| Official run | Mon 11 Jan 2027 – Fri 2 Apr 2027 (12 weeks, ~60 trading days) |
| Shakedown | Mon 14 Dec 2026 – Fri 8 Jan 2027, same configuration, data thrown away |
| Market | US equities, NYSE calendar |
| Starting capital | $100,000 per agent, USD |
| Decisions | Once per trading day after the close (some agents act only weekly or monthly). No intraday trading (D6) |
| Fills | Next trading day's opening price, in our own ledger (D1) |
| Side experiment | Two crypto agents, reported separately (§13, D7) |

## 2 · Universe (D4)

**Rule.** On the freeze date, download the holdings files of the 11 Select Sector SPDR ETFs (XLB, XLC, XLE, XLF, XLI, XLK, XLP, XLRE, XLU, XLV, XLY) from State Street. Take the **top 2 holdings by weight** of each ETF. Add **SPY**.

- A company with two share classes (e.g. GOOGL and GOOG) counts once: keep the class with the higher weight and take the next company.
- A company already chosen from another ETF is skipped; take the next one.
- Result: 22 stocks + SPY. Saved as `research/universe/universe.csv`, with the raw holdings files kept alongside as evidence.
- **Freeze date:** Sat 14 Nov 2026 (State Street holdings files of that date, saved in `research/universe/holdings_2026-11-14/`).

**Frozen universe** (all 23 tradable, shortable, easy to borrow and fractionable on Alpaca on the freeze date):

| ETF | Stocks |
|---|---|
| XLB Materials | LIN, NEM |
| XLC Communication | META, GOOGL |
| XLE Energy | XOM, CVX |
| XLF Financials | BRK.B, JPM |
| XLI Industrials | GE, CAT |
| XLK Technology | AAPL, NVDA |
| XLP Consumer Staples | WMT, COST |
| XLRE Real Estate | WELL, PLD |
| XLU Utilities | NEE, SO |
| XLV Health Care | LLY, JNJ |
| XLY Consumer Discretionary | AMZN, TSLA |
| Benchmark | SPY |

**During the run** the list never changes. If a stock is acquired or delisted, its position is closed at its last traded price and the cash stays in cash. It is not replaced.

## 3 · Daily timeline (all times US Eastern)

| Time (ET) | UK time* | Step |
|---|---|---|
| 16:00 | 21:00 | Market closes |
| 17:30 | 22:30 | Pull the day's final daily bars; run data checks |
| 17:45 | 22:45 | Build and store the snapshot (hashed, read-only) |
| 18:00 | 23:00 | All agents decide; risk checks; orders written to the ledger as *pending* |
| 09:30 next day | 14:30 | Pending orders filled at the official opening price |

\*UK times assume both countries are on standard time; they shift by an hour around the clock changes.

On early-close days the schedule runs from the early close. On market holidays nothing runs.

## 4 · The snapshot (no future data)

One snapshot per day, identical for every agent. It is built by a single function that can only read bars **timestamped at or before the close of the decision day** (CLAUDE.md rule 1), and tests must prove it cannot see the next day.

**For each of the 23 symbols:**
- Last close, and the last 20 closes
- Returns over 1, 5, 21 and 63 trading days
- 20-day and 100-day moving averages
- 20-day annualised volatility
- 20-day z-score of the close (distance from the 20-day mean, in standard deviations)
- Volume relative to its 20-day average

Prices are split-adjusted. Dividends are ignored for every agent alike (see §7).

**Rule-based agents** may compute anything else they need from up to 252 days of daily bars, using the same snapshot function.

**AI agents additionally receive (D5), and nothing else:**
- Their own cash, positions, weights, average cost and unrealised profit or loss
- Their own portfolio value and return since the start
- Their own last 5 decisions with the reasons they gave
- Their own limits (§6)
- Today's date

AI agents never see other agents' portfolios or performance, news, fundamentals, or the internet.

## 5 · Agents

All agents output **target weights** (fraction of current portfolio value per symbol, negative = short). The ledger turns targets into orders.

| # | Agent | Decides | Rule or instruction |
|---|---|---|---|
| 1 | Benchmark (SPY) | Day 1 only | 100% SPY, held for the whole run |
| 2 | Equal-weight hold | First trading day of each month | 1/22 in each stock |
| 3 | Momentum | First trading day of each week | Top 5 stocks by 63-day return, 20% each |
| 4 | Mean reversion | Daily | Long when z ≤ −1.5, short when z ≥ +1.5, exit when z crosses 0; 10% per position |
| 5 | Trend (MA crossover) | Daily | Equal weight in every stock whose 20-day MA is above its 100-day MA; the rest in cash |
| 6 | Long–short momentum | First trading day of each week | Long the top 3, short the bottom 3 by 63-day return, 1/6 each (net 0, gross 100%) |
| 7 | Constrained AI (Claude) | Daily | May only hold stocks from the current momentum top 5; chooses which, how much (0–20% each) and when, including holding all cash. Long only |
| 8 | Autonomous Claude | Daily | Free choice within Medium limits |
| 9 | Autonomous GPT | Daily | Identical prompt and snapshot to 8 |
| 10 | Autonomous Gemini | Daily | Identical prompt and snapshot to 8 |
| 11 | Autonomous DeepSeek | Daily | Identical prompt and snapshot to 8 |
| 12 | Autonomous Grok | Daily | Identical prompt and snapshot to 8 |
| 13 | Adaptive blender (Claude) | First trading day of each week | Allocates 0–70% to each of agents 3–6 (summing to 100%) and explains why; its portfolio is that blend of their target portfolios |
| 14 | AI Cautious (Claude) | Daily | Autonomous within Low limits; told to keep portfolio volatility below SPY's |
| 15 | AI Aggressive (Claude) | Daily | Autonomous within High limits; told to seek the highest return it can within them |

Agents 8–12 share one prompt template, word for word. Agents 7, 13, 14 and 15 have their own prompts, built from the same template (written together on Sat 5 Dec, stored in `agents/prompts/`).

## 6 · Risk limits

Checked after every decision, before any order. An order that would break a limit is **clipped** to the limit, and the clip is logged. Risk checks never add trades an agent didn't ask for, except forced reductions on a margin breach.

| Profile | Agents | Max per stock | Shorts | Max gross exposure | Max total short |
|---|---|---|---|---|---|
| Market | 1, 2 | 100% SPY / 1/22 each | No | 100% | 0% |
| Medium | 3–13 | 20% | Agents 4, 6, 8–12, and 13 through its blend of 4 and 6 | 100% | 30% (50% for agents 6 and 13) |
| Low | 14 | 10% | No | 100% | 0% |
| High | 15 | 30% | Yes | 150% | 50% |

AI agents are not told what other agents exist or how they are doing.

- **Shorts** are only allowed in stocks Alpaca flags easy-to-borrow at decision time. Otherwise the short is rejected and logged.
- **Margin:** if an agent's equity falls below 30% of its gross exposure, positions are cut pro rata until it's back above 40%.
- **No kill switch:** an agent that loses money keeps trading. Its losses are data.

## 7 · Execution and cost model (the ledger)

| Item | Model |
|---|---|
| Fill price | Next day's official open |
| Slippage | 5 basis points (0.05%) against the agent on every fill |
| Commission | $0 (as Alpaca) |
| Fractional shares | Allowed for longs; shorts in whole shares only (as Alpaca) |
| Borrow fee | 0.50% per year on the value of each short, charged daily. Alpaca charges $0 for easy-to-borrow stocks; we keep a small fee deliberately, to be conservative and closer to other brokers |
| Margin interest | 6.5% per year on borrowed cash, charged daily on a 360-day year (Alpaca's published non-Elite rate, checked 14 Nov 2026) |
| Interest on cash | None |
| Dividends | Ignored for every agent alike (simplification, stated in the paper) |
| Minimum trade | Trades under $100 are skipped, to avoid noise from tiny rebalances |

**Alpaca mirrors (D1):** agents 1, 8 and 13 also send the same orders to three Alpaca paper accounts at the open. Ledger and mirror are reconciled daily; differences are logged, never "fixed" in the ledger.

## 8 · AI settings

| Setting | Value |
|---|---|
| Access | All models through OpenRouter (D8), each **pinned to its official provider with fallbacks off**: Anthropic, OpenAI, Google, DeepSeek, xAI |
| Models | Claude, GPT, Gemini, DeepSeek, Grok (D9), chosen at **one price tier** (about $2 per million input tokens) rather than each maker's most expensive model, so cost doesn't decide the comparison |
| Model IDs | `anthropic/claude-sonnet-5.5` (Anthropic) · `openai/gpt-6.1-sol` (OpenAI) · `google/gemini-4-carbon` (Google) · `deepseek/deepseek-v4-pro-0813` (DeepSeek) · `x-ai/grok-4.7` (xAI). Checked by `scripts/test_pinned.py` |
| Data policy | OpenRouter's account setting allows providers that may keep or train on prompts, because DeepSeek's own API is one of them. Prompts contain only public market data and the prompt wording; nothing personal. Every other model is pinned to its maker, so this setting changes nothing for them |
| Reasoning | Each model's default reasoning setting; where a level must be chosen, **medium**. No "pro"/extended modes. The setting used is logged with every call |
| Agents 7, 13, 14, 15 and C1 | Same Claude model version as agent 8 |
| Temperature | 0 where the model allows it; otherwise the provider default, recorded |
| Tools | None: no web, no code execution |
| Calls | One call per agent per decision |
| Output | JSON: target weight per symbol, one-line reason per position, overall reasoning, confidence 0–1 |
| Invalid output | One retry with the validation error shown. Still invalid: hold current portfolio, log as `invalid` |
| Provider down | Three retries with back-off over 30 minutes. Still down: hold, log as `unavailable`. Never substitute another model or provider |
| Spend cap | One OpenRouter workspace budget: $15 a month, reviewed at the Wed 9 Dec cost check. Cap reached: hold, log as `capped` |

Every call is logged: prompt hash, model ID, the provider that actually served it, full response, tokens in and out, cost and latency. A call served by any other provider is treated as invalid.

## 9 · Logging (append-only)

One SQLite database. Nothing is ever edited or deleted (CLAUDE.md rule 2); corrections are new rows that reference the original.

| Table | One row per |
|---|---|
| `snapshots` | Trading day: hash, build time, data-check results |
| `decisions` | Agent per decision: snapshot id, raw response, parsed targets, reasons |
| `risk_events` | Clip, rejection or forced reduction |
| `orders` / `fills` | Order, and its fill with price, slippage and fees |
| `positions` / `equity` | Agent per day |
| `mirror_recon` | Mirrored agent per day: ledger vs Alpaca difference |
| `heartbeat` | Scheduled run: started, finished, errors |

The database is backed up off the device every night.

## 10 · Metrics and statistics

Computed after the run from the log, for every agent:

- **Return and risk:** daily returns, total return, annualised volatility
- **Risk-adjusted:** Sharpe ratio (risk-free rate = 3-month US Treasury bill yield on Mon 11 Jan 2027, held fixed), Sortino ratio, maximum drawdown, Calmar ratio
- **Market exposure:** CAPM alpha and beta, by regression of daily excess returns on SPY's
- **Behaviour:** turnover, average gross and net exposure, concentration (Herfindahl index), share of days in cash, short usage, risk-check clips
- **Explanations (RQ4):** tagging scheme built in Phase 6, before results are analysed

**Inference.** Differences in Sharpe ratio against the Benchmark get 95% confidence intervals from a block bootstrap (10,000 resamples, block length 5 days). With 14 comparisons, p-values are adjusted with the Holm method. Results are reported as effect sizes with intervals, whether or not they are "significant".

## 11 · Change control

| When | What may change |
|---|---|
| Until Gate 1 | Anything, by editing this file |
| Gate 1 → Sun 10 Jan | Changes allowed, each recorded in the changelog below, until the `protocol-v2.0` freeze |
| From Mon 11 Jan | **Nothing that changes a decision.** Bug fixes only, each logged in `research/changelog.md` with what broke, which days it affected and how. A change to any agent's config or prompt ends the experiment (CLAUDE.md rule 3) |

Artem starts and stops the official run himself, never Claude Code.

## 12 · Gate 1 record

Gate 1 was held on Sat 14 Nov 2026. All numbers approved; universe built by rule (§2); model lineup fixed (§8) and checked with `scripts/test_pinned.py`: all five served by their own maker (Anthropic, OpenAI, Google, DeepSeek, xAI). Tagged `protocol-v1.0`.

## 13 · Crypto side experiment (D7)

Reported separately. Never ranked or tested against the equity agents.

| Item | Value |
|---|---|
| Agents | **C0** Bitcoin benchmark: 100% BTC on day 1, held. **C1** AI crypto trader (Claude, same model as agent 8) |
| Coins | BTC, ETH, SOL, XRP, DOGE against USD (Alpha Arena's set minus BNB). Availability on Alpaca confirmed in Phase 1 |
| Capital | $100,000 each |
| Decisions | Once a day after the 00:00 UTC daily bar closes; filled at the next available price |
| Daily timings (UTC) | Every day, after the bar for day D closes at 00:00Z on D+1: **00:05Z** pull the final daily bars; **00:10Z** build and store the snapshot; **00:15Z** C0/C1 decide. UTC is used throughout, so US/UK clock changes don't move these times |
| C1 limits | Long only (Alpaca crypto can't be shorted or bought on margin); max 50% in one coin; may hold cash |
| C1 instruction | Aim for the highest return it can within the limits |
| C1 sees | The same kind of snapshot as the equity agents (returns, moving averages, volatility, z-scores) for the 5 coins, plus its own portfolio |
| Fees | 0.25% per trade (Alpaca's tier-1 taker fee), plus 5 bps slippage |
| Metrics | Return, volatility, Sharpe, drawdown; C1 compared with C0 only |

## Changelog

- **v0.1** · Sat 14 Nov 2026 · First draft from decisions D1–D5.
- **v0.2** · Sat 14 Nov 2026 · Gate 1 numbers approved. Applied D6 (daily only), D7 (crypto side experiment, new §13), D8 (OpenRouter, provider pinning), D9 (five models; Grok added as agent 12, DeepSeek replaces Kimi as 11; agents 12–14 renumbered 13–15). Margin interest set to Alpaca's 6.5%. Agent 7 may hold all cash; AI agents aren't told about other agents.
- **v1.0** · Sat 14 Nov 2026 · Gate 1. Universe frozen (22 stocks + SPY). Model lineup: Claude Sonnet 5.5, GPT-6.1 Sol, Gemini 4 Carbon, DeepSeek V4 Pro, Grok 4.7, matched on price tier; reasoning at default/medium.
- **v1.1** · Sat 14 Nov 2026 · Crypto daily timings fixed (fetch 00:05Z, snapshot 00:10Z, decide 00:15Z), added to §13.
