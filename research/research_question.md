# Research question

**Status:** v0.1 draft, Sat 14 Nov 2026. Frozen at Gate 1 together with the protocol.

## Primary question

> **When large language model agents and rule-based strategies trade the same US stocks with identical information and identical execution, how do they differ in risk-adjusted performance and risk-taking, and do the AI agents' written explanations match what they actually do?**

The question deliberately asks *how they differ*, not *which wins*. With about 60 trading days, a difference in returns between two agents will usually be within the noise. Behaviour (how much an agent trades, how concentrated it is, whether it obeys its risk instructions, whether its reasons fit the data) can be measured precisely in 60 days, so that is where the paper's strongest claims will come from.

## Sub-questions

### RQ1 · Strategies
Do AI agents that are **constrained** (agent 7), **autonomous** (agents 8–11) or **adaptive** (agent 12) differ from fixed rules (agents 1–6) in Sharpe ratio, maximum drawdown, turnover and concentration?

### RQ2 · Models
Given exactly the same prompt and snapshot, do **Claude, GPT, Gemini and Kimi** (agents 8–11) make systematically different decisions: different trade frequency, position sizes, sector tilts, or willingness to go to cash?

### RQ3 · Risk personas
When the same model is told to be **cautious** (agent 13) or **aggressive** (agent 14), does its realised risk actually change as instructed (volatility, concentration, leverage and short use), and does it stay inside its limits without the risk checks having to block it?

### RQ4 · Explanations
How often does an AI agent's stated reason for a trade match measurable facts in the snapshot it was given (for example, it says "strong momentum" and the stock really is in the top third by 3-month return)? Do explanations predict anything about the trade's outcome?

## Hypotheses (written before any data)

Written now so the results can't reshape them. Each one can be wrong.

- **H1.** No AI agent will beat the Benchmark (agent 1) on Sharpe ratio by a margin whose 95% confidence interval excludes zero. *(Expected: the run is too short to show significance.)*
- **H2.** The four autonomous models will differ more in **turnover** than in returns.
- **H3.** The Aggressive persona's realised volatility will be at least 1.5 times the Cautious persona's.
- **H4.** Most AI explanations will match the snapshot, but a measurable share (we'll report the percentage) will cite facts that are false or absent from it.
- **H5.** The Adaptive blender will hold, on average, more weight in whichever of agents 3–6 performed best over the previous four weeks (it chases recent winners).

## What this study can and can't show

- **Can:** describe and compare behaviour precisely; give effect sizes with bootstrap confidence intervals; measure explanation accuracy; show whether risk instructions are followed.
- **Can't:** prove long-run outperformance (12 weeks, one market period); generalise beyond US large caps; separate a model's "skill" from the exact model version, which providers keep updating. That's why versions are recorded and frozen.

## Why it matters

People are starting to hand trading and financial decisions to language models. Most published tests use backtests, where the model may already have seen the "future" in its training data. This run is **live and forward-looking**: no model can have seen prices from January to April 2027, so look-ahead through training data is impossible.
