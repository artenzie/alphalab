# Experiment protocol

**Version:** v1.10, Tue 17 Nov 2026 (Gate 1 + crypto timings + day-1 decisions + agent 4/5 clarifications + agent 4 capacity, pro rata clipping + ledger rules + cash floor + dust buys + complete targets + AI targets stocks only + AI call settings)
**Next:** final freeze as `protocol-v2.0` on Sun 10 Jan 2027, after the shakedown.

All numbers were approved at the Gate 1 review on Sat 14 Nov 2026. The universe and model lineup were filled in from the Gate 1 scripts the same evening. Everything follows from decisions D1–D9 in `research/decisions.md`.

---

## 1 · Overview

Fifteen equity agents (6 rule-based, 9 AI) each start with the same virtual capital. Every trading day after the US close, one snapshot of market data is built. Each agent sees the snapshot (and its own portfolio), outputs target portfolio weights, passes risk checks, and is filled at the next day's open in a simulated ledger. Every step is logged in an append-only database.

| Item | Value |
|---|---|
| Official run | Mon 11 Jan 2027 – Fri 2 Apr 2027 (12 weeks, 57 trading days; MLK Day 18 Jan, Presidents' Day 15 Feb and Good Friday 26 Mar 2027 are market holidays) |
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
| 4 | Mean reversion | Daily | Long when z ≤ −1.5, short when z ≥ +1.5, exit a long when z ≥ 0, a short when z ≤ 0. An entry gets 10% of current portfolio value. At most 10 open positions, of which at most 3 short; exits free their slots first, then entries fill free slots by the largest \|z\| first (ties by symbol), and the rest are skipped. No resizing: open positions keep the weight they have drifted to until they exit. Orders only for entries and exits; if there are none, it holds |
| 5 | Trend (MA crossover) | Daily | Equal weight in every stock whose 20-day MA is above its 100-day MA; the rest in cash. If the set of qualifying stocks is the same as the stocks it holds, it holds (no small daily rebalances) |
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

On the first day of the run every agent makes its first decision, including weekly and monthly ones.

Agents 3–6 trade only the 22 stocks, never SPY. AI agents (7–15) also target only the 22 stocks: SPY is in their snapshot as market context, and a target on SPY is invalid output (§8). Ranking ties are broken by symbol name (A before Z), so results are deterministic. A stock whose snapshot field for a rule is missing (too little history) is left out of that rule; agent 4 keeps an open position whose z-score is missing. Agents 2–6 also decide on any day when they hold none of the stocks yet, so a missed first decision is caught the next day.

Agents 8–12 share one prompt template, word for word. Agents 7, 13, 14 and 15 have their own prompts, built from the same template (written together on Sat 5 Dec, stored in `agents/prompts/`).

## 6 · Risk limits

Checked after every decision, before any order. An order that would break a limit is **clipped** to the limit, and the clip is logged. When a limit is breached, the positions on the breached side are scaled down **pro rata** until it holds: a per-stock limit cuts that position; the total-short limit scales all shorts; the gross limit scales all positions. Every clip is logged. Risk checks never add trades an agent didn't ask for, except forced reductions on a margin breach.

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

**Ledger rules** (v1.8; how the table above is applied):

1. **Sizing at the decision close.** Target shares = weight × portfolio value at the decision-day close ÷ that day's close, using exactly the closes in the snapshot. Order = target − current shares, so an unchanged weight creates no trade. A non-hold decision is the agent's complete target portfolio: a held symbol it leaves out has a target of 0 and is sold (or bought back), subject to the $100 minimum (rule 2). A hold trades nothing.
2. **Minimum trade.** An order worth under $100 at the decision close (the whole change in shares) is skipped.
3. **Rounding.** Long targets are rounded down to 9 decimals of a share; short targets are rounded towards zero to whole shares, after snapping a short within 10⁻⁹ of a whole share to it (so float noise never drops a share). A flip from long to short (or back) is two orders: close the old side, then open the new one.
4. **Fills.** Equities fill at the next trading day's official open with 5 bps slippage against the agent (buys pay more, sells receive less). Crypto (§13) fills at the next available price after the decision time, plus the 0.25% fee and 5 bps slippage.
5. **Cash.** Short sale proceeds go to cash; cash earns nothing. Sizing at the close and filling at the open can leave a fully invested agent a little below zero cash, which pays margin interest like any other borrowing; agents under the cash floor (rule 12) never borrow this way.
6. **Daily accruals, every calendar day, 360-day year.** Borrow fee 0.50%/yr on short market value; margin interest 6.5%/yr on negative cash. Charged in arrears at each close: every calendar day since the previous close is charged on the balances at that previous close (Monday's close charges Friday, Saturday and Sunday on Friday's balances).
7. **Mark to market** at each close: equity = cash + Σ shares × close. Dividends are ignored.
8. **Margin.** The §6 margin check runs at each close, before the decision. If the agent holds, the forced reductions become orders and are filled like any other. If the agent gives new targets, those (risk-checked) targets replace the forced reduction, because they already satisfy the margin rule; the breach is still logged.
9. **Delisted or acquired stock:** closed at its last traded price (with the usual slippage); the cash stays cash.
10. **Restart safety.** If fills already exist for an agent and fill date, that day is not filled again.
11. **No early prices.** The fill for decision day D uses D+1's open only once D+1's bar has closed and is stored (the same closed-bar rule as the ingest). A stock split between decision and fill stops the ledger for a manual correction rather than being sized across.
12. **Cash floor** (agents whose gross limit is 100% and who can't short: 1, 2, 3, 5, 7, 14, C0, C1). At the open, sells are filled first. If the buys would then take cash below 0, all that day's buys are scaled down pro rata (shares rounded down) so cash ends at 0, and each scaled buy is logged as a risk event (`cash_floor`). A scaled buy is filled even if it falls under $100, but a buy scaled to under $1 is cancelled (a `cancelled` fill) and its `cash_floor` event says so. Agents allowed more than 100% gross (15) and short-selling agents are not floored; margin interest applies to any negative cash they have.

**Alpaca mirrors (D1):** agents 1, 8 and 13 also send the same orders to three Alpaca paper accounts at the open. Ledger and mirror are reconciled daily; differences are logged, never "fixed" in the ledger.

## 8 · AI settings

| Setting | Value |
|---|---|
| Access | All models through OpenRouter (D8), each **pinned to its official provider with fallbacks off**: Anthropic, OpenAI, Google, DeepSeek, xAI. Pinned to the provider's **standard endpoint** (OpenRouter tags `anthropic`, `openai`, `google-ai-studio` / `google-vertex`, `deepseek`, `xai`), not its flex, fast or priority tiers, so price and latency don't vary by tier. Parameters a provider doesn't support are rejected, never silently dropped |
| Models | Claude, GPT, Gemini, DeepSeek, Grok (D9), chosen at **one price tier** (about $2 per million input tokens) rather than each maker's most expensive model, so cost doesn't decide the comparison |
| Model IDs | `anthropic/claude-sonnet-5.5` (Anthropic) · `openai/gpt-6.1-sol` (OpenAI) · `google/gemini-4-carbon` (Google) · `deepseek/deepseek-v4-pro-0813` (DeepSeek) · `x-ai/grok-4.7` (xAI). Checked by `scripts/test_pinned.py` |
| Data policy | OpenRouter's account setting allows providers that may keep or train on prompts, because DeepSeek's own API is one of them. Prompts contain only public market data and the prompt wording; nothing personal. Every other model is pinned to its maker, so this setting changes nothing for them |
| Reasoning | Effort **medium**, set explicitly for all five models, so no model runs without reasoning while another reasons by default. No "pro"/extended modes. The setting used is logged with every call |
| Agents 7, 13, 14, 15 and C1 | Same Claude model version as agent 8 |
| Temperature | 0 where the model allows it (Gemini, DeepSeek, Grok); otherwise not sent, so the provider default applies (Claude, GPT), recorded per call |
| Max output | 16,000 tokens per call for all five models, reasoning included. A reply cut off at the limit is invalid output |
| Tools | None: no web, no code execution |
| Calls | One call per agent per decision |
| Output | JSON: target weight per symbol, one-line reason per position, overall reasoning, confidence 0–1 |
| Invalid output | One retry with the validation error shown. Still invalid: hold current portfolio, log as `invalid` |
| Provider down | Three retries with back-off over 30 minutes. Still down: hold, log as `unavailable`. Never substitute another model or provider |
| Spend cap | One OpenRouter workspace budget: $15 a month, reviewed at the Wed 9 Dec cost check. Cap reached: hold, log as `capped` |

Every call is logged: prompt hash, model ID, the provider that actually served it, full response, tokens in and out, cost and latency. A call served by any other provider, or reporting a model other than the requested ID or a dated variant of it, is treated as invalid (both IDs logged).

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
- **v1.1 (clerical)** · Sat 14 Nov 2026 · Removed a repeated sentence; trading-day count corrected to 57 from the cached calendar; §1 names all three market holidays in the run (MLK Day 18 Jan, Presidents' Day 15 Feb, Good Friday 26 Mar 2027). No rule changed.
- **v1.2** · Sat 14 Nov 2026 · §5: on the first day of the run every agent makes its first decision, including weekly and monthly ones (agent 2's first month, Jan 2027, starts during the shakedown). Day 1 is the decision on Mon 11 Jan after the close, filled at the Tue 12 Jan open (§3).
- **v1.3** · Sat 14 Nov 2026 · §5 clarifications, written down before agents 3–6 were coded; no number changed. Agent 4: open positions that aren't exiting keep their 10% target, and it holds when nothing enters or exits. Agent 5: it holds when the set of qualifying stocks hasn't changed. Agents 3–6: only the 22 stocks, ties broken by symbol name, stocks with a missing field are left out, and a not-yet-invested portfolio is caught the next day.
- **v1.4** · Sat 14 Nov 2026 · §5 agent 4: capacity of 10 open positions (10 × 10% = 100% gross), at most 3 shorts (30%); when entry signals exceed free slots, the largest |z_20| enters first, ties by symbol, the rest are skipped; an entry gets 10% of current portfolio value; no resizing, so open positions keep their drifted weight until they exit (replaces the v1.3 "keep their 10% target"). §6: a breached limit is fixed by scaling the breached side down pro rata, every clip logged. Decided after the 57-session trade count showed agent 4 at up to 20 positions, 200% gross, with 719 of 842 orders being resizes.
- **v1.5** · Sun 15 Nov 2026 · §7 ledger rules written down before the ledger was coded; no number changed. Sizing at the decision close; $100 minimum on the whole order; long shares to 9 decimals, shorts whole shares towards zero; flips as two orders; crypto fills at the next price after decide time; accruals every calendar day on a 360-day year, charged in arrears; margin cut replaced by a same-day non-hold decision; delisting at the last price; restart safety; no fill before the next bar is closed and stored; a split between decision and fill stops the ledger.
- **v1.6** · Sun 15 Nov 2026 · §7 ledger rule 12, cash floor: long-only agents with a 100% gross limit (1, 2, 3, 5, 7, 14, C0, C1) fill sells first and scale the day's buys down pro rata so a fill never takes cash below 0; logged as `cash_floor` risk events. Rule 5 notes the exception. Prompted by the smoke run, where agent 2's day-1 buys at the open left cash at −$69.51.
- **v1.7** · Sun 15 Nov 2026 · §7 rule 12: a buy the cash floor scales to under $1 is cancelled instead of filling a few billionths of a share; its `cash_floor` event says so. The cash it would have used stays as cash. Follows the dust-buy note in the 15 Nov log.
- **v1.8** · Sun 15 Nov 2026 · §7 rule 1: a non-hold decision is the complete target portfolio; a held symbol left out goes to 0 (replaces "a symbol an agent leaves out keeps its shares"). The §6 risk checks therefore see the whole resulting portfolio. Prompted by the first two-year control backtest, where agents 3–6 never sold stocks they dropped (agent 6 reached 2.9× gross, agent 5's buys went unfunded every day).
- **v1.8 (clerical)** · Sun 15 Nov 2026 · §7 rule 3: a short target within 10⁻⁹ of a whole share is snapped to it before rounding towards zero. Without this, float noise turned an unchanged −10-share target into −9 in about 10% of cases (a spurious resize of agent 4's kept shorts). No number changed.
- **v1.9** · Tue 17 Nov 2026 · §5: AI agents (7–15) may target only the 22 stocks. SPY stays in their snapshot as market context; a target on SPY is invalid output (§8: one retry with the error shown, then hold). SPY is the benchmark every agent is measured against, not a holding for the agents being compared to it. Written down before the AI harness was coded; no number changed.
- **v1.10** · Tue 17 Nov 2026 · §8 call settings, fixed before the OpenRouter client was coded. (1) Each model is pinned to its provider's standard endpoint, not flex/fast/priority tiers, and unsupported parameters are rejected rather than dropped. (2) Max output 16,000 tokens for all five, reasoning included, so a runaway reasoning trace can't run up the bill; a cut-off reply is invalid. (3) Reasoning effort **medium** for all five, replacing "each model's default". Fairness: the defaults differ by maker (some models reason by default, others answer without reasoning unless asked), so "default" would have compared reasoning models with non-reasoning ones and confounded the model comparison with a setting. One explicit level puts every model on the same footing. Temperature is unchanged: 0 where accepted (Gemini, DeepSeek, Grok); Claude and GPT accept none, so it isn't sent and is recorded as the provider default. (4) A reply reporting a model other than the requested ID or a dated variant of it is invalid, like a wrong provider; both IDs are logged.
