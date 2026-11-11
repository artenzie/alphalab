# AlphaLab

AlphaLab is a research project that runs AI trading agents against Alpaca **paper** trading and measures how they perform.

- Official start date: **TBD** (set this before the experiment begins; rule 3 applies from that date on)
- Secrets live in `.env` (gitignored). Code reads them with `python-dotenv` + `os.getenv()` (see `test_alpaca.py`).
- Python env: `venv/` (gitignored).

## Layout

| Folder | Purpose |
|---|---|
| `research/logs/` | Dated learning logs (`YYYY-MM-DD.md`) |
| `agents/prompts/` | Agent configs and prompts |
| `strategies/` | Strategy definitions |
| `data/` | Market data and snapshots |
| `execution/` | Order placement / broker integration |
| `analysis/` | Performance analysis |
| `dashboard/` | Monitoring UI |
| `tests/` | Tests |

## Non-negotiable rules

1. **Never use future data in any agent input.** Agents may only see data timestamped at or before the moment they decide. Look-ahead bias makes every result meaningless.
2. **The decision log is append-only.** Never edit, reorder, or delete past entries. Corrections go in as new entries that reference the original.
3. **Never change agent configs or prompts after the official start date.** They are frozen for the whole experiment. Changing them means starting a new experiment.
4. **Never print, log, or commit API keys.** Don't echo or cat `.env`. Show variable names only, with the values redacted. Before each commit, check the staged diff for anything that looks like a secret.
5. **Show a plan before changing the ledger, snapshot, or protocol.** Wait for explicit approval before any edit to them.
6. **Commit after every task.** Each commit is small and has a clear message.
