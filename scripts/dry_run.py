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


# ---------------------------------------------------------------------------
# Checks after the replay
# ---------------------------------------------------------------------------

def _final_rows(conn, ac: str, ai_only_13: bool = True) -> dict[tuple[str, str], sqlite3.Row]:
    """(agent, snapshot_date) -> its final decision row (agent 13: its last AI call row if ai_only_13)."""
    out = {}
    for r in conn.execute(
            """SELECT d.*, s.snapshot_date FROM decisions d JOIN snapshots s USING (snapshot_id)
               WHERE s.asset_class = ? AND d.corrects_id IS NULL ORDER BY s.snapshot_date, d.attempt""", (ac,)):
        if ai_only_13 and r["agent_id"] == "13" and r["model_id"] is None:
            continue
        out[(r["agent_id"], r["snapshot_date"])] = r
    return out


def check_settle(conn) -> list[str]:
    from runner import roster
    lines, bad = [], 0
    for ac, ids in (("us_equity", roster.EQUITY_RULE + roster.EQUITY_AI), ("crypto", roster.CRYPTO_RULE + roster.CRYPTO_AI)):
        days = [r[0] for r in conn.execute("SELECT snapshot_date FROM snapshots WHERE asset_class = ? ORDER BY 1", (ac,))]
        for i, d in enumerate(days):
            marks = {r[0]: r for r in conn.execute("SELECT agent_id, equity, borrow_fee_usd, margin_interest_usd FROM equity "
                                                   "WHERE as_of_date = ?", (d,)) if r[0] in ids}
            missing = sorted(set(ids) - set(marks))
            prev_orders = 0 if i == 0 else conn.execute(
                f"""SELECT COUNT(*) FROM orders o JOIN agents a USING (agent_id) WHERE a.asset_class = ? AND o.venue = 'ledger'
                    AND o.created_at >= ? AND o.created_at < ?""",
                (ac, days[i - 1] + "T12:00:00.000Z", d + "T12:00:00.000Z")).fetchone()[0]
            unfilled = 0 if i == 0 else conn.execute(
                f"""SELECT COUNT(*) FROM orders o JOIN agents a USING (agent_id) WHERE a.asset_class = ? AND o.venue = 'ledger'
                    AND o.created_at >= ? AND o.created_at < ? AND NOT EXISTS (SELECT 1 FROM fills f WHERE f.order_id = o.order_id)""",
                (ac, days[i - 1] + "T12:00:00.000Z", d + "T12:00:00.000Z")).fetchone()[0]
            fees = sum((m["borrow_fee_usd"] or 0) + (m["margin_interest_usd"] or 0) for m in marks.values())
            bad += bool(missing) + bool(unfilled)
            lines.append(f"  {ac:<9} {d}  marks {len(marks)}/{len(ids)}"
                         + (f" MISSING {missing}" if missing else "")
                         + f"  previous day's orders {prev_orders}, unfilled {unfilled}  accruals ${fees:.2f}")
    errs = conn.execute("SELECT job, run_id FROM heartbeat WHERE event = 'error'").fetchall()
    lines.append(f"  heartbeat errors: {len(errs)}" + (f" {[e[0] for e in errs]}" if errs else ""))
    return lines, bad + len(errs)


def check_last_decisions(conn, record: Path) -> tuple[list[str], int]:
    bad, n = 0, 0
    lines = []
    for ac, tag in (("us_equity", "equity"), ("crypto", "crypto")):
        final = _final_rows(conn, ac)
        for f in sorted(record.glob(f"{tag}_*_*_*.txt")):
            _, d, aid, k = f.stem.split("_")
            inp = json.loads(f.read_text(encoding="utf-8").split("\nINPUT\n", 1)[1].split("\n", 1)[0])
            want = sorted((dd for (a, dd) in final if a == aid and dd < d))[-5:]
            got = [x["date"] for x in inp["last_decisions"]]
            ok = got == want
            for x in inp["last_decisions"]:
                r = final[(aid, x["date"])]
                if aid == "13":
                    alloc = json.loads(r["parsed_allocation"]) if r["status"] == "ok" and r["parsed_allocation"] else None
                    ok &= (x["allocation"] is None) == (alloc is None) and x["status"] == r["status"]
                else:
                    t = json.loads(r["parsed_targets"]) if r["status"] == "ok" and r["parsed_targets"] else None
                    ok &= x["status"] == r["status"] and (x["targets"] is None) == (t is None)
                    if t is not None:
                        ok &= all(abs(x["targets"][s] - round(w, 6)) < 1e-9 for s, w in t.items()) and set(x["targets"]) == set(t)
            n += 1
            if not ok:
                bad += 1
                lines.append(f"  MISMATCH {f.name}: prompt {got}, log {want}")
    lines.insert(0, f"  {n} prompts checked, {bad} mismatches")
    return lines, bad


def check_agent7(conn) -> list[str]:
    from execution import ledger_db as LD
    from strategies.base import ret63_top
    lines = []
    for sid, d, payload in conn.execute("SELECT snapshot_id, snapshot_date, payload FROM snapshots "
                                        "WHERE asset_class = 'us_equity' ORDER BY 2").fetchall():
        class _S:
            pass
        snap = _S()
        snap.payload = json.loads(payload)
        top = ret63_top(snap)
        held = sorted(LD.load_account(conn, "7", as_of=date.fromisoformat(d)).positions)
        off = [s for s in held if s not in top]
        ev = [(r[0], r[1]) for r in conn.execute("SELECT r.symbol, r.detail FROM risk_events r JOIN decisions x "
                                                 "USING (decision_id) WHERE r.agent_id = '7' AND r.rule = 'eligibility' "
                                                 "AND x.snapshot_id = ?", (sid,))]
        sells = [r[0] for r in conn.execute("SELECT o.symbol FROM orders o JOIN decisions x USING (decision_id) "
                                            "WHERE o.agent_id = '7' AND o.side = 'sell' AND x.snapshot_id = ?", (sid,))]
        lines.append(f"  {d}  top 5 {top}  held {held}  left the list {off or 'none'}  "
                     f"eligibility events {[e[0] for e in ev] or 'none'}  sells {sells or 'none'}")
    return lines


def check_agent13(conn) -> list[str]:
    lines = []
    for sid, d in conn.execute("SELECT snapshot_id, snapshot_date FROM snapshots WHERE asset_class = 'us_equity' ORDER BY 2"):
        traded = [k for k in ("3", "4", "5", "6") if (r := conn.execute(
            "SELECT status, parsed_targets FROM decisions WHERE agent_id = ? AND snapshot_id = ? ORDER BY attempt DESC LIMIT 1",
            (k, sid)).fetchone()) and r[0] == "ok" and json.loads(r[1]) is not None]
        rows = conn.execute("SELECT attempt, model_id, status, parsed_targets, overall_reasoning FROM decisions "
                            "WHERE agent_id = '13' AND snapshot_id = ? ORDER BY attempt", (sid,)).fetchall()
        kinds = []
        for r in rows:
            if r[1] is not None:
                kinds.append(f"AI call ({r[2]}{', hold' if json.loads(r[3] or 'null') is None else ', allocate'})")
            elif json.loads(r[3]) is None:
                kinds.append("hold row")
            else:
                kinds.append("follow row")
        lines.append(f"  {d} ({date.fromisoformat(d):%a})  3-6 traded: {traded or 'none'}  agent 13: {', '.join(kinds)}")
    return lines


def check_cost(conn) -> list[str]:
    lines, tot = [], 0.0
    for ac, d, cost, calls in conn.execute(
            """SELECT s.asset_class, s.snapshot_date, SUM(d.cost_usd), COUNT(d.model_id) FROM decisions d
               JOIN snapshots s USING (snapshot_id) GROUP BY 1, 2 ORDER BY 2, 1"""):
        tot += cost or 0
        lines.append(f"  {ac:<9} {d}  ${cost or 0:.4f}  ({calls} recorded calls)")
    lines.append(f"  total recorded: ${tot:.4f}")
    return lines


def report(path: Path, record: Path) -> str:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        out = ["1. Settlement"]
        lines, bad1 = check_settle(conn)
        out += lines
        lines, bad2 = check_last_decisions(conn, record)
        out += ["2. last_decisions in every saved prompt"] + lines
        out += ["3. Agent 7"] + check_agent7(conn)
        out += ["4. Agent 13"] + check_agent13(conn)
        out += ["6. Cost"] + check_cost(conn)
        return "\n".join(out)
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
    r = sub.add_parser("report")
    r.add_argument("--db", type=Path, required=True)
    r.add_argument("--record", type=Path, required=True)
    a = p.parse_args(argv)
    try:
        if a.cmd == "report":
            print(report(a.db, a.record))
            return 0
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
