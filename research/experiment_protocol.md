# Experiment protocol

**Version:** v0.1 draft, Sat 14 Nov 2026
**Next:** reviewed line by line at Gate 1, then tagged `protocol-v1.0`. Final freeze as `protocol-v2.0` on Sun 10 Jan 2027, after the shakedown.

Numbers marked **[G1]** are proposals to confirm or change at the Gate 1 review. Everything else follows from decisions D1–D5 in `research/decisions.md`.

---

## 1 · Overview

Fourteen agents (6 rule-based, 8 AI) each start with the same virtual capital. Every trading day after the US close, one snapshot of market data is built. Each agent sees the snapshot (and its own portfolio), outputs target portfolio weights, passes risk checks, and is filled at the next day's open in a simulated ledger. Every step is logged in an append-only database.

| Item | Value |
|---|---|
| Official run | Mon 11 Jan 2027 – Fri 2 Apr 2027 (12 weeks, ~60 trading days) |
| Shakedown | Mon 14 Dec 2026 – Fri 8 Jan 2027, same configuration, data thrown away |
| Market | US equities, NYSE calendar |
| Starting capital | $100,000 per agent, USD |
| Decisions | Once per trading day after the close (some agents act only weekly or monthly) |
| Fills | Next trading day's opening price, in our own ledger (D1) |

## 2 · Universe (D4)

**Rule.** On the freeze date, download the holdings files of the 11 Select Sector SPDR ETFs (XLB, XLC, XLE, XLF, XLI, XLK, XLP, XLRE, XLU, XLV, XLY) from State Street. Take the **top 2 holdings by weight** of each ETF. Add **SPY**.

- A company with two share classes (e.g. GOOGL and GOOG) counts once: keep the class with the higher weight and take the next company.
- A company already chosen from another ETF is skipped; take the next one.
- Result: 22 stocks + SPY. Saved as `research/universe/universe.csv`, with the raw holdings files kept alongside as evidence.
- **Freeze date:** the Gate 1 review day **[G1]**.

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
| 3 | Momentum | First trading day of each week | Top 5 stocks by 63-day return, 20% each **[G1]** |
| 4 | Mean reversion | Daily | Long when z ≤ −1.5, short when z ≥ +1.5, exit when z crosses 0; 10% per position **[G1]** |
| 5 | Trend (MA crossover) | Daily | Equal weight in every stock whose 20-day MA is above its 100-day MA; the rest in cash |
| 6 | Long–short momentum | First trading day of each week | Long the top 3, short the bottom 3 by 63-day return, 1/6 each (net 0, gross 100%) |
| 7 | Constrained AI (Claude) | Daily | May only hold stocks from the current momentum top 5; chooses which, how much (0–20% each) and when. Long only |
| 8 | Autonomous Claude | Daily | Free choice within Medium limits |
| 9 | Autonomous GPT | Daily | Identical prompt and snapshot to 8 |
| 10 | Autonomous Gemini | Daily | Identical prompt and snapshot to 8 |
| 11 | Autonomous Kimi | Daily | Identical prompt and snapshot to 8 |
| 12 | Adaptive blender (Claude) | First trading day of each week | Allocates 0–70% to each of agents 3–6 (summing to 100%) and explains why; its portfolio is that blend of their target portfolios |
| 13 | AI Cautious (Claude) | Daily | Autonomous within Low limits; told to keep portfolio volatility below SPY's |
| 14 | AI Aggressive (Claude) | Daily | Autonomous within High limits; told to seek the highest return it can within them |

Agents 8–11 share one prompt template, word for word. Agents 7, 12, 13 and 14 have their own prompts, built from the same template (written together on Sat 5 Dec, stored in `agents/prompts/`).

## 6 · Risk limits

Checked after every decision, before any order. An order that would break a limit is **clipped** to the limit, and the clip is logged. Risk checks never add trades an agent didn't ask for, except forced reductions on a margin breach.

| Profile | Agents | Max per stock | Shorts | Max gross exposure | Max total short |
|---|---|---|---|---|---|
| Market | 1, 2 | 100% SPY / 1/22 each | No | 100% | 0% |
| Medium | 3–12 | 20% | Agents 4, 6, 8–11, and 12 through its blend of 4 and 6 | 100% | 30% (50% for agents 6 and 12) |
| Low | 13 | 10% | No | 100% | 0% |
| High | 14 | 30% | Yes | 150% | 50% |

All limits **[G1]**.

- **Shorts** are only allowed in stocks Alpaca flags easy-to-borrow at decision time. Otherwise the short is rejected and logged.
- **Margin:** if an agent's equity falls below 30% of its gross exposure, positions are cut pro rata until it's back above 40% **[G1]**.
- **No kill switch:** an agent that loses money keeps trading. Its losses are data.

## 7 · Execution and cost model (the ledger)

| Item | Model |
|---|---|
| Fill price | Next day's official open |
| Slippage | 5 basis points (0.05%) against the agent on every fill **[G1]** |
| Commission | $0 (as Alpaca) |
| Fractional shares | Allowed for longs; shorts in whole shares only (as Alpaca) |
| Borrow fee | 0.50% per year on the value of each short, charged daily **[G1]** |
| Margin interest | 6.0% per year on borrowed cash, charged daily **[G1]**; check Alpaca's current rate |
| Interest on cash | None |
| Dividends | Ignored for every agent alike (simplification, stated in the paper) |
| Minimum trade | Trades under $100 are skipped, to avoid noise from tiny rebalances |

**Alpaca mirrors (D1):** agents 1, 8 and 12 also send the same orders to three Alpaca paper accounts at the open. Ledger and mirror are reconciled daily; differences are logged, never "fixed" in the ledger.

## 8 · AI settings

| Setting | Value |
|---|---|
| Models | One per provider: Anthropic (Claude), OpenAI (GPT), Google (Gemini), Moonshot (Kimi). **Exact model version strings recorded and frozen at Gate 1** |
| Agents 7, 12, 13, 14 | Same Claude model version as agent 8 |
| Temperature | 0 where the model allows it; otherwise the provider default, recorded |
| Tools | None: no web, no code execution |
| Calls | One call per agent per decision |
| Output | JSON: target weight per symbol, one-line reason per position, overall reasoning, confidence 0–1 |
| Invalid output | One retry with the validation error shown. Still invalid: hold current portfolio, log as `invalid` |
| Provider down | Three retries with back-off over 30 minutes. Still down: hold, log as `unavailable`. Never substitute another model |
| Spend caps | Monthly cap per provider **[G1]**, set after the Wed 9 Dec cost check. Cap reached: hold, log as `capped` |

Every call is logged: prompt hash, full response, tokens in and out, cost and latency.

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

**Inference.** Differences in Sharpe ratio against the Benchmark get 95% confidence intervals from a block bootstrap (10,000 resamples, block length 5 days) **[G1]**. With 13 comparisons, p-values are adjusted with the Holm method. Results are reported as effect sizes with intervals, whether or not they are "significant".

## 11 · Change control

| When | What may change |
|---|---|
| Until Gate 1 | Anything, by editing this file |
| Gate 1 → Sun 10 Jan | Changes allowed, each recorded in the changelog below, until the `protocol-v2.0` freeze |
| From Mon 11 Jan | **Nothing that changes a decision.** Bug fixes only, each logged in `research/changelog.md` with what broke, which days it affected and how. A change to any agent's config or prompt ends the experiment (CLAUDE.md rule 3) |

Artem starts and stops the official run himself, never Claude Code.

## 12 · Open items for the Gate 1 review

1. Confirm every **[G1]** number above.
2. Record the exact model version strings for all four providers.
3. Run the universe rule and save the list and holdings files.
4. Decide whether agent 7 may also exit to cash completely (current draft: yes, weights may sum to less than 100%).
5. Decide whether the AI agents should be told what kind of agents they are competing with (current draft: no).
6. Check the borrow-fee and margin-interest assumptions against Alpaca's current published rates.

## Changelog

- **v0.1** · Sat 14 Nov 2026 · First draft from decisions D1–D5.
