# AlphaLab

AlphaLab is a research project that runs AI trading agents against Alpaca **paper** trading and measures how they perform.

- Official start date: **Mon 11 Jan 2027** (D3; rule 3 applies from that date on)
- Secrets live in `.env` (gitignored). Code reads them with `python-dotenv` + `os.getenv()` (see `test_alpaca.py`).
- Python env: `venv/` (gitignored).

## Layout

| Path | Purpose |
|---|---|
| `research/experiment_protocol.md` | The protocol (currently **v1.15**); § numbers in code refer to it |
| `research/decisions.md`, `research/logs/` | Design decisions (D1, D2, …) and dated learning logs |
| `research/universe/` | Frozen universe (`universe.csv`) + holdings evidence |
| `data/db.py` | SQLite schema setup (`schema.sql`) and connections; append-only triggers |
| `data/ingest.py` | Daily Alpaca bars into versioned `prices` rows; never stores unclosed bars |
| `data/fill_prices.py` | Crypto 1-minute fill bars for the hour [t_ref, t_ref+60 min) after each day's crypto decision time into `crypto_fill_bars`, plus `crypto_fill_windows` once the hour is fetched in full; closed bars only, never in a snapshot |
| `data/calendar.py` | NYSE trading days, closes, daily run times (all UTC) |
| `data/checks.py` | Data checks on stored bars, run after every ingest |
| `data/snapshot.py` | Point-in-time daily snapshot: the only data an agent sees |
| `strategies/base.py` | Agent interface: `decide(snapshot, portfolio, context) -> Decision` |
| `strategies/registry.py` | Agent id -> decide function |
| `strategies/benchmark.py` … | Agents 1-6: benchmark, equal_weight, momentum, mean_reversion, trend, long_short; `bitcoin.py` is C0 (§13) |
| `execution/risk.py` | §6 risk checks (`apply_risk`, `margin_check`, agent 7's `apply_eligibility`), pure functions |
| `execution/ledger.py` | §7 ledger accounting core (orders, fills, accruals, marks, margin, delisting), pure functions |
| `execution/ledger_db.py` | Ledger on the append-only tables: `settle_day` (fill at open, or crypto at the first 1-min bar after the decision; accrue, mark), `place_orders` |
| `scripts/` | One-off tools: build_universe, OpenRouter model checks (`test_pinned`, `smoke_ai`), backtest_indicators/risk/ledger. Never collected by pytest (`pytest.ini`) |
| `tests/` | pytest suite |
| `agents/harness.py` | `AIAgent`: same `decide` interface; prompt (template + output spec + JSON input incl. `run`, and `eligible` for agent 7), JSON validation, §8 retries, call record per attempt; crypto spec for C1 |
| `agents/prompts/`, `agents/templates.py` | The real prompt templates (README there: how they're assembled) and their loader; `python -m agents.templates` prints each agent's template SHA-256 |
| `agents/llm.py` | `LLMClient` interface, `FakeClient` for tests |
| `agents/models.py` | Frozen §8 model lineup as data (agent → model, endpoints, reasoning, temperature) |
| `agents/openrouter.py` | `OpenRouterClient`: pinned endpoints, no fallbacks, error mapping, key never logged |
| `runner/daily.py` | `python -m runner.daily equity\|crypto --experiment NAME [--date D]`: one trading day in protocol order (ingest → settle → snapshot → decide → risk → orders), restart-safe, heartbeat per step, late runs (v1.13) |
| `runner/experiment.py`, `runner/roster.py` | One DB file per experiment (`data/<name>.db`, labelled with start and end date; `create --start --end` copies market data, empty ledger); which agents run (all but 13) and how they're built |
| `analysis/results.py` | Results report per agent from an experiment DB: returns, late and missed days, `--exclude-late` (§10) |
| `dashboard/` | Not built yet |

## Working here

- Run tests: `venv/bin/python -m pytest`
- Use plan mode for anything touching the ledger, snapshot, risk checks or protocol.

## Non-negotiable rules

1. **Never use future data in any agent input.** Agents may only see data timestamped at or before the moment they decide. Look-ahead bias makes every result meaningless.
2. **The decision log is append-only.** Never edit, reorder, or delete past entries. Corrections go in as new entries that reference the original.
3. **Never change agent configs or prompts after the official start date.** They are frozen for the whole experiment. Changing them means starting a new experiment.
4. **Never print, log, or commit API keys.** Don't echo or cat `.env`. Show variable names only, with the values redacted. Before each commit, check the staged diff for anything that looks like a secret.
5. **Show a plan before changing the ledger, snapshot, or protocol.** Wait for explicit approval before any edit to them.
6. **Commit after every task.** Each commit is small and has a clear message.
