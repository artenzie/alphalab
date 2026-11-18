# Experiment protocol

**Version:** v1.14, Wed 18 Nov 2026 (Gate 1 + crypto timings + day-1 decisions + agent 4/5 clarifications + agent 4 capacity, pro rata clipping + ledger rules + cash floor + dust buys + complete targets + AI targets stocks only + AI call settings + reasoning described + crypto fill price + late runs, one database per experiment, late crypto fill window + reasoning rule, Claude reasoning gate, herding metric)
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

**Late runs** (v1.13). If a scheduled run is missed, the runner may run late, as long as the agents decide at least 1 hour before the next session's open. In a late run the snapshot's cutoff is the time it is actually built (not close + 1:45), and the snapshot is labelled late with the minutes late; a heartbeat records it. Data rules and everything else are unchanged; equity fill timing is unchanged, and a late crypto decision fills after its actual decision time (§7 rule 4). After that deadline the day is a hold for every agent: the ledger still settles and marks, and the margin check still runs.

## 4 · The snapshot (no future data)

One snapshot per day, identical for every agent. It is built by a single function that can only read bars **timestamped at or before the close of the decision day** (CLAUDE.md rule 1), and tests must prove it cannot see the next day. Its cutoff is the protocol snapshot time (§3, §13), or the actual build time in a late run (§3).

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
4. **Fills.** Equities fill at the next trading day's official open with 5 bps slippage against the agent (buys pay more, sells receive less). Crypto (§13) fills at the next available price after the decision time, plus the 0.25% fee and 5 bps slippage: the **open of the first 1-minute bar in the hour that starts at the decision time** t_ref, i.e. in [t_ref, t_ref + 60 min). t_ref is the time the day's last crypto decision was actually recorded (never the scheduled 00:15Z), rounded up to the whole minute; it is one time per day, shared by every crypto agent including the BTC buy-and-hold control, so all their orders are written together at t_ref (v1.13). Once a day's crypto orders are placed its t_ref is fixed: a crypto agent that decides after that (for example in a rerun after it failed) is missed for the day, with no order, labelled late, rather than getting a second t_ref. A crypto fill is therefore never earlier than its order. If a coin has no bar in that hour once the whole hour has been fetched, its order is cancelled and logged (no fill). A decision recorded after the next day's snapshot cutoff (00:10Z on D+2) is missed: no trade, labelled late; the §3 deadline (23:15Z on D+1) normally stops a run well before that. The minute bars come from their own table (`crypto_fill_bars`), never from the daily bars, and no agent ever sees them. For crypto, day D+1 fills decision D's orders and marks at D+1's daily close (00:00Z on D+2); every calendar day is a settle day.
5. **Cash.** Short sale proceeds go to cash; cash earns nothing. Sizing at the close and filling at the open can leave a fully invested agent a little below zero cash, which pays margin interest like any other borrowing; agents under the cash floor (rule 12) never borrow this way.
6. **Daily accruals, every calendar day, 360-day year.** Borrow fee 0.50%/yr on short market value; margin interest 6.5%/yr on negative cash. Charged in arrears at each close: every calendar day since the previous close is charged on the balances at that previous close (Monday's close charges Friday, Saturday and Sunday on Friday's balances).
7. **Mark to market** at each close: equity = cash + Σ shares × close. Dividends are ignored.
8. **Margin.** The §6 margin check runs at each close, before the decision. If the agent holds, the forced reductions become orders and are filled like any other. If the agent gives new targets, those (risk-checked) targets replace the forced reduction, because they already satisfy the margin rule; the breach is still logged.
9. **Delisted or acquired stock:** closed at its last traded price (with the usual slippage); the cash stays cash.
10. **Restart safety.** If fills already exist for an agent and fill date, that day is not filled again.
11. **No early prices.** The fill for decision day D uses D+1's open only once D+1's bar has closed and is stored (the same closed-bar rule as the ingest). A crypto fill uses a minute bar only once that minute has ended and the bar was fetched by the time of settlement. A stock split between decision and fill stops the ledger for a manual correction rather than being sized across.
12. **Cash floor** (agents whose gross limit is 100% and who can't short: 1, 2, 3, 5, 7, 14, C0, C1). At the open, sells are filled first. If the buys would then take cash below 0, all that day's buys are scaled down pro rata (shares rounded down) so cash ends at 0, and each scaled buy is logged as a risk event (`cash_floor`). A scaled buy is filled even if it falls under $100, but a buy scaled to under $1 is cancelled (a `cancelled` fill) and its `cash_floor` event says so. Agents allowed more than 100% gross (15) and short-selling agents are not floored; margin interest applies to any negative cash they have.

**Alpaca mirrors (D1):** agents 1, 8 and 13 also send the same orders to three Alpaca paper accounts at the open. Ledger and mirror are reconciled daily; differences are logged, never "fixed" in the ledger.

## 8 · AI settings

| Setting | Value |
|---|---|
| Access | All models through OpenRouter (D8), each **pinned to its official provider with fallbacks off**: Anthropic, OpenAI, Google, DeepSeek, xAI. Pinned to the provider's **standard endpoint** (OpenRouter tags `anthropic`, `openai`, `google-ai-studio` / `google-vertex`, `deepseek`, `xai`), not its flex, fast or priority tiers, so price and latency don't vary by tier. Parameters a provider doesn't support are rejected, never silently dropped |
| Models | Claude, GPT, Gemini, DeepSeek, Grok (D9), chosen at **one price tier** (about $2 per million input tokens) rather than each maker's most expensive model, so cost doesn't decide the comparison |
| Model IDs | `anthropic/claude-sonnet-5.5` (Anthropic) · `openai/gpt-6.1-sol` (OpenAI) · `google/gemini-4-carbon` (Google) · `deepseek/deepseek-v4-pro-0813` (DeepSeek) · `x-ai/grok-4.7` (xAI). Checked by `scripts/test_pinned.py` |
| Data policy | OpenRouter's account setting allows providers that may keep or train on prompts, because DeepSeek's own API is one of them. Prompts contain only public market data and the prompt wording; nothing personal. Every other model is pinned to its maker, so this setting changes nothing for them |
| Reasoning | Effort **medium**, set explicitly for all five models. No "pro"/extended modes. How much a model reasons at that level is its own choice and is logged with every call (reasoning tokens). Claude Sonnet 5.5 uses adaptive thinking: reasoning is always available but can't be forced or turned off, and at medium it may answer without reasoning when it judges the task easy. It did so on the 13 Nov smoke prompt (0 reasoning tokens), while it reasoned on harder tasks at medium and on the same prompt at high. "Medium" is each maker's own scale, not a common token budget (13 Nov smoke run: GPT 105, Claude 0, Gemini 1,298, Grok 2,916, DeepSeek 5,261 reasoning tokens). **Rule (v1.14):** effort medium for every model; the amount of reasoning is an outcome, not a setting. It is logged per call, and every model comparison reports reasoning tokens per model (median, and the share of calls with any reasoning) |
| Claude reasoning gate (v1.14, pre-registered) | During the shakedown (Mon 14 Dec 2026 to Fri 8 Jan 2027), every equity decision day also makes one **shadow call**: Claude Sonnet 5.5 at effort **high**, with exactly agent 8's prompt (same snapshot, portfolio and memory). It is never traded and never shown to any agent; its targets go through agent 8's risk checks and are stored in `shadow_decisions`. **Gate**, computed on Sat 9 Jan 2027 over every shakedown day on which both have targets: the mean pairwise target overlap (§10) between the shadow book and agent 8's real targets. **Below 0.6:** Claude moves to effort high for all its agents (7, 8, 13, 14, 15, C1) before the Sun 10 Jan freeze. **0.6 or above:** Claude stays at medium. The result and the number are recorded in the changelog either way; no other outcome is considered |
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

One SQLite database file per experiment (v1.13): the shakedown and the official run each have their own, labelled with the experiment's name and start date, and every ledger starts empty at $100,000 per agent. Nothing is ever edited or deleted (CLAUDE.md rule 2); corrections are new rows that reference the original.

| Table | One row per |
|---|---|
| `snapshots` | Trading day: hash, build time, data-check results |
| `decisions` | Agent per decision: snapshot id, raw response, parsed targets, reasons |
| `risk_events` | Clip, rejection or forced reduction |
| `orders` / `fills` | Order, and its fill with price, slippage and fees |
| `positions` / `equity` | Agent per day |
| `mirror_recon` | Mirrored agent per day: ledger vs Alpaca difference |
| `heartbeat` | Scheduled run: started, finished, errors |
| `crypto_fill_bars` | Crypto coin per closed 1-minute bar in the hour [t_ref, t_ref + 60 min) after each day's crypto decision time t_ref (00:15Z on D+1 when on time; v1.13: later for a late run), versioned like daily bars: the fill price source, never in a snapshot |
| `crypto_fill_windows` | Fill hour fetched in full after it closed (v1.13): the evidence that a coin with no bar in it had no trades, so its order is cancelled rather than left waiting |
| `shadow_decisions` | Shakedown only (v1.14): the daily untraded Claude-at-high shadow call on agent 8's prompt, per attempt, with its risk-checked targets: the data for the §8 Claude reasoning gate |

**Known limitation (v1.13, Wed 18 Nov 2026).** A crypto fill may fall after the day's 00:00Z mark: the fill hour [t_ref, t_ref + 60 min) of a decision recorded after 23:00Z on D+1 reaches past D+1's daily close, so if no minute bar exists between t_ref and 00:00Z the order fills on a bar after the close at which that day was marked. It needs a late run near the 23:15Z deadline and a coin with no trade for the rest of that hour; it is left as is and any such fill is visible in `fills.filled_at`.

The database is backed up off the device every night.

## 10 · Metrics and statistics

Computed after the run from the log, for every agent:

- **Return and risk:** daily returns, total return, annualised volatility
- **Late and missed days (v1.13):** every report counts each agent's late days (decided on a late snapshot, §3) and missed days (no decision), and can be recomputed without the late days: a day's return is left out when the decision behind it (the previous day's) was late
- **Risk-adjusted:** Sharpe ratio (risk-free rate = 3-month US Treasury bill yield on Mon 11 Jan 2027, held fixed), Sortino ratio, maximum drawdown, Calmar ratio
- **Market exposure:** CAPM alpha and beta, by regression of daily excess returns on SPY's
- **Behaviour:** turnover, average gross and net exposure, concentration (Herfindahl index), share of days in cash, short usage, risk-check clips, pairwise AI target overlap (v1.14: per day and pair of AI agents, the same-direction sum of min weights of their risk-checked targets, longs with longs and shorts with shorts; 0 = nothing in common, 1 = the same fully invested book; a hold keeps the last targets), measuring herding from day one
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
| Decisions | Once a day after the 00:00 UTC daily bar closes; filled at the open of the first 1-minute bar at or after the decision time (§7 rule 4) |
| Daily timings (UTC) | Every day, after the bar for day D closes at 00:00Z on D+1: **00:05Z** pull the final daily bars; **00:10Z** build and store the snapshot; **00:15Z** C0/C1 decide. UTC is used throughout, so US/UK clock changes don't move these times. A missed run may run late (as §3), deciding at least 1 hour before the next 00:15Z decision, i.e. by 23:15Z on D+1 |
| C1 limits | Long only (Alpaca crypto can't be shorted or bought on margin); max 50% in one coin; may hold cash |
| C1 instruction | Aim for the highest return it can within the limits |
| C1 sees | The same kind of snapshot as the equity agents (returns, moving averages, volatility, z-scores) for the 5 coins, plus its own portfolio |
| Fees | 0.25% per trade (Alpaca's tier-1 taker fee), plus 5 bps slippage. Because the fee is paid on top, a 100% buy always costs more than the cash, so the cash floor (§7 rule 12) scales C0's and a fully invested C1's buys; each such scale is logged as `cash_floor` |
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
- **v1.11** · Tue 17 Nov 2026 · §8 Reasoning row describes what effort medium actually does; the setting is unchanged (medium for all five). Claude Sonnet 5.5 uses adaptive thinking (OpenRouter: reasoning mandatory, default effort high): at medium it reasoned on a hard puzzle (1,480 tokens) but not on the 13 Nov trading prompt (0), and reasoned on that prompt at high (327). The response showed no reasoning at all (null, 0 billed reasoning tokens), not reasoning that was hidden. Medium was kept so every model gets the same instruction; how often each chooses to reason is measured, not tuned. Reasoning tokens are logged per call. Diagnostic in the 17 Nov log.
- **v1.12** · Tue 17 Nov 2026 · §7 rule 4, rule 11, §9, §13: the crypto fill price, fixed before the crypto fill was coded. A crypto order fills at the open of the first 1-minute bar starting at or after the later of 00:15Z and the time the orders were written (rounded up to the minute), plus the 0.25% fee and 5 bps, so a fill never predates its order (an AI call can return after 00:15Z). The minute bars from 00:15Z to 01:15Z are stored in a separate append-only table `crypto_fill_bars`, only once closed, and never enter a snapshot. For crypto, day D+1 fills decision D and marks at D+1's daily close. Noted: with the fee on top, the cash floor scales every 100% crypto buy. No number changed.
- **v1.13** · Wed 18 Nov 2026 · §3, §4, §9, §13: (1) Late runs. A missed scheduled run may run late if the agents decide at least 1 hour before the next session's open (equity) or the next 00:15Z decision (crypto). In a late run the snapshot cutoff is its actual build time and the snapshot is labelled late with the minutes late (a heartbeat records it); data rules and fill timing are unchanged. Past the deadline the day is a hold for every agent, as before. Previously a late run's snapshot excluded bars fetched after the protocol cutoff, failed the stale check, and the day was lost. (2) One database file per experiment (shakedown, official), each labelled with its name and start date, so the official run starts every ledger at $100,000 on Mon 11 Jan with no shakedown rows in it. Decided with the daily runner. (3) Late crypto fill window (§7 rule 4, §9, §10; added the same day, Wed 18 Nov 2026). The crypto fill hour was fixed at 00:15Z to 01:15Z on D+1, so a late crypto decision (say 03:09Z) had no minute bar to fill at and its orders could never settle. Now the hour starts at t_ref, the time the day's last crypto decision was actually recorded (one t_ref per day, shared by C0 and C1, all crypto orders written at it): orders fill at the first minute bar in [t_ref, t_ref + 60 min); a coin with no bar in a fully fetched hour has its order cancelled and logged; a decision recorded after the next day's snapshot cutoff, or after the day's crypto orders were already placed, is missed (no trade, labelled late). Known limitation noted in §9: a late fill can fall after the 00:00Z mark. An on-time day settles exactly as under v1.12 (regression test against v1.12 ledger output). Results reports count late days and can exclude them (§10).
- **v1.14** · Wed 18 Nov 2026 · §8, §9, §10: (1) Reasoning rule: effort medium for every model; the amount of reasoning is an outcome, logged per call and reported per model with every comparison. Reason: on the first real run (18 Nov) Claude used 0 reasoning tokens against 128 to 6,744 for the others; a matched token budget can't be set for all five (only Anthropic and Google take a native budget, and it is a cap, not a floor), and per-model tuning would replace one instruction with five judgement calls. (2) Claude reasoning gate, pre-registered before the shakedown: a daily untraded shadow call (Claude at effort high, agent 8's exact prompt) during the shakedown; if the mean overlap between the shadow book and agent 8's targets is below 0.6, all Claude agents move to effort high before the 10 Jan freeze, otherwise they stay at medium. (3) §10 adds pairwise AI target overlap (herding) to the behaviour metrics; on 18 Nov the mean over the 10 pairs was 0.73 (placeholder prompts). (4) §9 table: `shadow_decisions`.
