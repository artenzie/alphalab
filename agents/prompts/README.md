# AI agent prompts (protocol §5, §8, §13)

Written Sat 21 Nov 2026, in one sitting, so every prompt shares one template. Frozen with the protocol on Sun 10 Jan 2027 (CLAUDE.md rule 3): after that, any change ends the experiment.

## How a prompt is assembled

prompt = template + `"\n\n"` + output spec + `"\n\nINPUT\n"` + compact JSON input (as `agents/harness.py` does today).

| Agent | Template | Output spec |
|---|---|---|
| 7 Constrained Claude | `equity_common.md` + `"\n\n"` + `goal_constrained.md` | `OUTPUT_SPEC` |
| 8–12 Autonomous (all five models) | `equity_common.md` + `"\n\n"` + `goal_autonomous.md` | `OUTPUT_SPEC` |
| 13 Adaptive blender | `agent_13_blender.md` | `OUTPUT_SPEC_BLENDER` (below) |
| 14 Cautious | `equity_common.md` + `"\n\n"` + `goal_cautious.md` | `OUTPUT_SPEC` |
| 15 Aggressive | `equity_common.md` + `"\n\n"` + `goal_aggressive.md` | `OUTPUT_SPEC` |
| C1 Crypto | `crypto_c1.md` | `OUTPUT_SPEC_CRYPTO` |

Files are read as UTF-8 and stripped of trailing whitespace. Agents 8–12 get byte-identical templates; only the model differs. Each template's SHA-256 is recorded with every call (`prompt_hash` covers the full prompt) and listed in the protocol at the freeze.

No template contains a date or an experiment name, so the shakedown and the official run use byte-identical templates. Everything that changes per day or per experiment is in the input.

## Design decisions (Artem, Sat 21 Nov 2026)

1. **Goal for agents 7, 8–12 and 13:** "the best return you can for the risk you take, over the whole run". This matches the paper's main metric (Sharpe) and places 8–12 between Cautious (14: volatility below SPY's) and Aggressive (15: highest return).
2. **Horizon:** the agents are told the run's end date and the trading days left (`input.run`).
3. **Framing:** honest. "An AI agent taking part in a research experiment", simulated paper money, no real money at stake. There is no role-play persona.
4. **Agent 7's rule:** a target outside `input.eligible` makes the whole reply invalid (one retry with the error shown, then hold), the same as a target on SPY. A held stock that has left the list is sold at the next open even on a hold, and the sale is logged.

These were decided without discussion, because they follow from the protocol or keep the comparison fair:
- Every prompt states the real costs and limits (§6, §7), and gives the same data dictionary for the snapshot fields (§4).
- Agents are not told any other participant exists (§6). Agent 13 is the one exception: it sees agents 3–6, because that is its job.
- There is no instruction to "think step by step". Reasoning is set by `reasoning.effort` (§8), not by the prompt.
- The prompts don't forbid using background knowledge from training. They only say the input is all the market data the agent gets. This is unavoidable, and it's discussed as a limitation in the paper.
- "Give the reasons you actually used" is the only instruction about explanations (RQ4). It doesn't ask for any particular style.

## New input keys (added to `prompt_input`)

- `run` (every AI agent): `{"end_date": "YYYY-MM-DD", "trading_days_left": N}`. `end_date` is the experiment's last decision day, from its label. `trading_days_left` counts the run's trading days after today: equity uses NYSE days, crypto uses calendar days. It is 0 on the last day.
- `eligible` (agent 7 only): today's top 5 stocks by `ret_63`, highest first, ties by symbol. This is exactly agent 3's ranking (`strategies.base.rank`). There are fewer than 5 if fewer stocks have `ret_63`.
- Agent 13: `strategies`, `allocation`. See the template and `OUTPUT_SPEC_BLENDER`.

## OUTPUT_SPEC_BLENDER (agent 13)

~~~
OUTPUT FORMAT
Reply with one JSON object and nothing else (a ```json fenced block is also accepted). Either:
  {"action": "hold", "reasoning": "<why>"}
to keep your current allocation, or:
  {"action": "allocate", "allocation": {"3": <w>, "4": <w>, "5": <w>, "6": <w>}, "reasoning": "<overall reasoning>", "confidence": <0 to 1>}
Rules:
- allocation names all four strategies, "3", "4", "5" and "6", and nothing else.
- Each weight is from 0 to 0.70, and the four add up to 1.
- confidence is a number from 0 to 1.
~~~

Validation: all four keys present, no others; each weight finite and in [0, 0.70]; the sum is within 1e-6 of 1. Otherwise the reply is invalid (one retry, then hold). Before agent 13's first decision its allocation is "none" (all cash), so a hold on day 1 stays in cash.
