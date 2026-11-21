"""Dry run: replay past days through the real daily runner on a scratch `dev` database.

    python scripts/dry_run.py build --source data/alphalab.db --out <scratch>/dry.db --start 2026-11-11 --end 2027-01-08
    python scripts/dry_run.py day   --db <scratch>/dry.db --asset-class equity --date 2026-11-11 --record <scratch>/prompts
    python scripts/dry_run.py all   --db <scratch>/dry.db --record <scratch>/prompts --equity 2026-11-11,... --crypto 2026-11-11,...

Why a replay database (dry run plan, Sat 21 Nov 2026). Every bar in alphalab.db was fetched
after the fact (11-13 Nov on 14 Nov, 14-17 Nov on 17 Nov), so the live snapshot (fetched_at <=
cutoff) and the ledger's no-early-prices reads (fetched_at <= now) would see nothing on time.
Instead of changing snapshot.py or ledger_db.py, `build` writes a new database whose market data
looks ingested on time: the latest version of each daily bar gets fetched_at = its protocol fetch
time (equity: session close + 1:30; crypto: 00:05Z on D+1), each crypto fill bar fetched_at = the
end of its hour, and one crypto_fill_windows row per fetched hour. Every stamp is at or after the
bar's close.

Rule 1 then holds with no code changed: the production snapshot (live mode: bar_date <= D, session
closed by the cutoff, newest version with fetched_at <= cutoff) and ledger guards run as they are,
against a replay clock that starts at each day's protocol times. What is not reproduced: the
values are as fetched on 14 / 17 Nov, with that day's split adjustment (the data checks report
splits), and the easy-to-borrow flags are today's (fetched once, reused every day).

`day` runs runner.daily.run_day with the replay clock, a no-op ingest and fill fetch (the data is
already stored), the real OpenRouter client (every prompt saved under --record), no Alpaca
orders. Refused unless the label is `dev`: never on official or shakedown.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import calendar as cal  # noqa: E402
from data import db  # noqa: E402
from runner import daily  # noqa: E402
from runner import experiment as ex  # noqa: E402

DRY_LABEL = "dev"
PRICE_COLS = ("symbol", "asset_class", "bar_date", "bar_ts", "open", "high", "low", "close", "volume",
              "adj_open", "adj_high", "adj_low", "adj_close", "adj_volume", "vwap", "trade_count", "feed")
FILL_COLS = ("symbol", "decision_date", "window_start", "bar_ts", "open", "high", "low", "close", "volume", "feed")


class DryRunError(Exception):
    pass


def on_time_fetch(conn: sqlite3.Connection, asset_class: str, bar_date: date) -> datetime:
    """The protocol fetch time of a daily bar: equity close + 1:30 (§3), crypto 00:05Z on D+1 (§13)."""
    if asset_class == "crypto":
        return cal.crypto_times(bar_date).bars_fetch_at
    return cal.equity_times(bar_date, conn, _no_network).bars_fetch_at


def _no_network():
    raise RuntimeError("dry run: the calendar must come from the cache")


def build(source: Path, out: Path, start: date, end: date) -> ex.Experiment:
    """A new `dev` database with source's market data stamped as if fetched on time."""
    if out.exists():
        raise DryRunError(f"{out} already exists")
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    conn = db.init_db(out)
    try:
        with conn:
            for table in ("calendar_fetches", "market_sessions"):           # copied as is
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
                rows = src.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY rowid").fetchall()
                conn.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                 [tuple(r) for r in rows])
            latest = src.execute(
                f"""SELECT {', '.join(PRICE_COLS)} FROM prices AS p WHERE p.fetched_at =
                    (SELECT MAX(q.fetched_at) FROM prices AS q WHERE q.symbol = p.symbol AND q.bar_date = p.bar_date)
                    ORDER BY p.asset_class, p.bar_date, p.symbol""").fetchall()
            stamps: dict[tuple[str, str], str] = {}
            rows = []
            for r in latest:
                key = (r["asset_class"], r["bar_date"])
                if key not in stamps:
                    stamps[key] = db.utc_ts(on_time_fetch(conn, r["asset_class"], date.fromisoformat(r["bar_date"])))
                rows.append((*tuple(r), stamps[key]))
            conn.executemany(f"INSERT INTO prices ({', '.join(PRICE_COLS)}, fetched_at) VALUES "
                             f"({', '.join('?' * (len(PRICE_COLS) + 1))})", rows)
            fills = src.execute(
                f"""SELECT {', '.join(FILL_COLS)} FROM crypto_fill_bars AS b WHERE b.fetched_at =
                    (SELECT MAX(q.fetched_at) FROM crypto_fill_bars AS q WHERE q.symbol = b.symbol AND q.bar_ts = b.bar_ts)
                    ORDER BY b.window_start, b.symbol, b.bar_ts""").fetchall()
            windows: dict[tuple[str, str], set[str]] = {}
            for r in fills:
                windows.setdefault((r["decision_date"], r["window_start"]), set()).add(r["symbol"])
            end_of = {k: db.utc_ts(cal._parse_ts(k[1]) + timedelta(minutes=60)) for k in windows}
            conn.executemany(f"INSERT INTO crypto_fill_bars ({', '.join(FILL_COLS)}, fetched_at) VALUES "
                             f"({', '.join('?' * (len(FILL_COLS) + 1))})",
                             [(*tuple(r), end_of[(r["decision_date"], r["window_start"])]) for r in fills])
            conn.executemany("""INSERT INTO crypto_fill_windows (decision_date, window_start, window_end, symbols,
                                fetched_at, run_id) VALUES (?, ?, ?, ?, ?, 'dry_run_build')""",
                             [(d, ws, end_of[(d, ws)], json.dumps(sorted(syms)), end_of[(d, ws)])
                              for (d, ws), syms in sorted(windows.items())])
            conn.execute("INSERT INTO experiment (name, start_date, end_date, source) VALUES (?, ?, ?, ?)",
                         (DRY_LABEL, start.isoformat(), end.isoformat(), f"{source.name} (dry-run replay)"))
        return ex.require(conn)
    finally:
        conn.close()
        src.close()


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

class ReplayClock:
    """The runner's clock in a replay: starts at a given time, moves only when the runner waits."""

    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += timedelta(seconds=s)


class Recorder:
    """Wraps the real client; saves every prompt it sends (market data and wording only)."""

    def __init__(self, inner, agent_id: str, folder: Path, tag: str):
        self.inner, self.agent_id, self.folder, self.tag = inner, agent_id, folder, tag

    def complete(self, prompt, *, model):
        self.folder.mkdir(parents=True, exist_ok=True)
        n = len(list(self.folder.glob(f"{self.tag}_{self.agent_id}_*.txt"))) + 1
        (self.folder / f"{self.tag}_{self.agent_id}_{n}.txt").write_text(prompt, encoding="utf-8")
        return self.inner.complete(prompt, model=model)


def require_dev(conn: sqlite3.Connection) -> ex.Experiment:
    exp = ex.require(conn)
    if exp.name != DRY_LABEL:
        raise DryRunError(f"the dry run only runs on a {DRY_LABEL!r} database, not {exp.name!r}")
    return exp


def etb_flags(cache: Path) -> dict[str, bool]:
    """Today's easy-to-borrow flags from Alpaca, fetched once and reused (no historical flags exist)."""
    if cache.exists():
        return json.loads(cache.read_text())
    from dotenv import load_dotenv
    from data import ingest
    load_dotenv(ROOT / ".env")
    clients = ingest.make_clients()
    import time
    flags = {}
    for s in ingest.load_universe():
        if s == "SPY":
            continue
        a = ingest.with_retries(lambda: clients.trading.get_asset(s), f"get_asset {s}", time.sleep)
        flags[s] = bool(getattr(a, "easy_to_borrow", False))
    cache.write_text(json.dumps(flags, sort_keys=True))
    return flags


def replay_deps(conn, asset_class: str, day: date, record: Path, flags: dict[str, bool],
                start_at: datetime | None = None, client_factory=None) -> daily.Deps:
    times = cal.crypto_times(day) if asset_class == "crypto" else cal.equity_times(day, conn, _no_network)
    clock = ReplayClock(start_at or times.bars_fetch_at)
    tag = f"{'crypto' if asset_class == 'crypto' else 'equity'}_{day.isoformat()}"
    if client_factory is None:
        from dotenv import load_dotenv
        from agents.openrouter import OpenRouterClient
        load_dotenv(ROOT / ".env")
        client_factory = lambda aid: OpenRouterClient()      # noqa: E731
    return daily.Deps(now=clock, sleep=clock.sleep,
                      ingest=lambda conn_, ac, run_id, now: 0,               # stored already, stamped on time
                      fill_prices=lambda conn_, dates, run_id, now, ws=None: 0,
                      etb=lambda syms: {s: flags.get(s, False) for s in syms},
                      client_for=lambda aid: Recorder(client_factory(aid), aid, record, tag),
                      trading=_no_network, ai_sleep=lambda s: None)


def run_one(path: Path, asset_class: str, day: date, record: Path, flags: dict[str, bool],
            start_at: datetime | None = None, client_factory=None) -> daily.RunReport:
    conn = db.connect(path)
    try:
        require_dev(conn)
        ac = "crypto" if asset_class == "crypto" else "us_equity"
        rep = daily.run_day(conn, ac, day, replay_deps(conn, ac, day, record, flags, start_at, client_factory))
        print(daily.format_report(rep))
        return rep
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Dry run on a replay dev database.")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--source", type=Path, required=True)
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--start", type=date.fromisoformat, required=True)
    b.add_argument("--end", type=date.fromisoformat, required=True)
    d = sub.add_parser("day")
    d.add_argument("--db", type=Path, required=True)
    d.add_argument("--asset-class", choices=("equity", "crypto"), required=True)
    d.add_argument("--date", type=date.fromisoformat, required=True)
    d.add_argument("--record", type=Path, required=True)
    d.add_argument("--at", default=None, help="start the replay clock here (ISO, UTC) instead of the fetch time")
    a = p.parse_args(argv)
    try:
        if a.cmd == "build":
            exp = build(a.source, a.out, a.start, a.end)
            print(f"built {a.out}: {exp.name} {exp.start_date} to {exp.end_date}")
            return 0
        flags = etb_flags(a.record.parent / "etb_flags.json")
        at = None if a.at is None else datetime.fromisoformat(a.at).astimezone(timezone.utc)
        return run_one(a.db, a.asset_class, a.date, a.record, flags, at).exit_code
    except (DryRunError, ex.ExperimentError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
