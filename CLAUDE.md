# AlphaLab

AlphaLab is a research project that runs AI trading agents against Alpaca **paper** trading and measures how they perform.

- Official start date: **Mon 11 Jan 2027** (D3; rule 3 applies from that date on)
- Secrets live in `.env` (gitignored). Code reads them with `python-dotenv` + `os.getenv()` (see `test_alpaca.py`).
- Python env: `venv/` (gitignored).

## Layout

| Path | Purpose |
|---|---|
| `research/experiment_protocol.md` | The protocol (currently **v1.4**); § numbers in code refer to it |
| `research/decisions.md`, `research/logs/` | Design decisions (D1, D2, …) and dated learning logs |
| `research/universe/` | Frozen universe (`universe.csv`) + holdings evidence |
| `data/db.py` | SQLite schema setup (`schema.sql`) and connections; append-only triggers |
| `data/ingest.py` | Daily Alpaca bars into versioned `prices` rows; never stores unclosed bars |
| `data/calendar.py` | NYSE trading days, closes, daily run times (all UTC) |
| `data/checks.py` | Data checks on stored bars, run after every ingest |
| `data/snapshot.py` | Point-in-time daily snapshot: the only data an agent sees |
| `strategies/base.py` | Agent interface: `decide(snapshot, portfolio, context) -> Decision` |
| `strategies/registry.py` | Agent id -> decide function |
| `strategies/benchmark.py` … | Agents 1-6: benchmark, equal_weight, momentum, mean_reversion, trend, long_short |
| `execution/risk.py` | §6 risk checks (`apply_risk`, `margin_check`), pure functions |
| `scripts/` | One-off tools: build_universe, OpenRouter model checks, backtest_indicators/risk |
| `tests/` | pytest suite |
| `agents/prompts/`, `analysis/`, `dashboard/` | Not built yet |

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
