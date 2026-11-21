"""Unattended scheduling: run whatever is due now for the active experiment (protocol §3, §13, v1.13).

    python -m runner.tick                         # what the systemd timer runs every 5 minutes
    python -m runner.tick activate dev --no-ai    # write data/active_experiment (dev labels only)
    python -m runner.tick deactivate              # empty it: ticks do nothing

The active experiment is the one line in data/active_experiment (gitignored): `<name>` or
`<name> --no-ai`. Only Artem writes it for shakedown or official (§11): `activate` refuses
those names, and a tick refuses --no-ai on any label but dev.

A day is due, per asset class, when it is in the run (start <= day <= end; an NYSE session for
equity, every calendar day for crypto), its protocol fetch time has passed (data/calendar.py, all
UTC, so clock changes and early closes need no config) and it has no daily_run that finished
with status ok. Then, counting daily_run starts (not ticks):
  * before the day's decision deadline (v1.13): at most MAX_ATTEMPTS starts. An on-time or late
    run; each restart-safe (stored decisions are never called again, placed orders skipped).
  * after the deadline: one settle-only run (run_day settles, marks and runs the margin check;
    nobody decides), then the day is left alone and runner.status shows it as a gap.
Due days run oldest first, so a Pi that was down catches up with no holes in the ledger.

A lock (data/tick.lock) keeps two ticks apart; each run also takes runner.daily's per-asset
lock, so a manual `runner.daily` run is never overlapped. Each tick writes one heartbeat row
(job 'tick') to the experiment's database, so gaps between ticks measure uptime (D2).
Exit code 1 if any run ended in error or the tick failed; the next timer tick is the retry.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import sqlite3
import sys
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from data import calendar as cal
from data import db
from runner import daily
from runner import experiment as ex

MAX_ATTEMPTS = 3                    # daily_run starts per day and asset class before the deadline
ACTIVE_NAME = "active_experiment"
LOCK_NAME = "tick.lock"
DEV = "dev"
PROTECTED = (ex.OFFICIAL, ex.SHAKEDOWN)
ASSETS = ("us_equity", "crypto")
_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")


class TickError(Exception):
    pass


@dataclass(frozen=True)
class Active:
    name: str
    no_ai: bool = False


def read_active(data_dir: Path) -> Active | None:
    """The active experiment, or None if the file is missing or empty."""
    path = data_dir / ACTIVE_NAME
    if not path.exists():
        return None
    words = path.read_text(encoding="utf-8").split()
    if not words:
        return None
    name, flags = words[0], words[1:]
    if not _NAME.match(name):
        raise TickError(f"{path}: bad experiment name {name!r}")
    unknown = [f for f in flags if f != "--no-ai"]
    if unknown:
        raise TickError(f"{path}: unknown flags {unknown} (only --no-ai)")
    return Active(name, "--no-ai" in flags)


def write_active(data_dir: Path, name: str, no_ai: bool) -> Path:
    """`activate`: dev labels only. Shakedown and official are switched by Artem by hand (§11)."""
    if name in PROTECTED:
        raise TickError(f"the code never activates {name!r} (§11); write the file yourself: "
                        f"echo {name} > {data_dir / ACTIVE_NAME}")
    path = data_dir / f"{name}.db"
    if not path.exists():
        raise TickError(f"{path} not found (create it with python -m runner.experiment create)")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        exp = ex.require(conn)
    finally:
        conn.close()
    if exp.name != DEV:
        raise TickError(f"{path} is labelled {exp.name!r}; `activate` only writes dev experiments")
    (data_dir / ACTIVE_NAME).write_text(f"{name}{' --no-ai' if no_ai else ''}\n", encoding="utf-8")
    return data_dir / ACTIVE_NAME


# ---------------------------------------------------------------------------
# What is due
# ---------------------------------------------------------------------------

def _starts(conn: sqlite3.Connection, asset_class: str, day: date) -> list[tuple[str, datetime]]:
    """(run_id, started at) of every daily_run for this asset class and day."""
    rows = conn.execute(
        """SELECT run_id, at FROM heartbeat WHERE job = ? AND event = 'started'
             AND json_extract(detail, '$.asset_class') = ? AND json_extract(detail, '$.date') = ?
           ORDER BY heartbeat_id""", (daily.JOB, asset_class, day.isoformat())).fetchall()
    return [(r[0], cal._parse_ts(r[1])) for r in rows]


def finished_ok(conn: sqlite3.Connection, asset_class: str, day: date) -> bool:
    ids = [r for r, _ in _starts(conn, asset_class, day)]
    if not ids:
        return False
    names = ", ".join("?" * len(ids))
    return conn.execute(
        f"""SELECT 1 FROM heartbeat WHERE job = ? AND event = 'finished' AND run_id IN ({names})
              AND json_extract(detail, '$.status') = 'ok' LIMIT 1""", (daily.JOB, *ids)).fetchone() is not None


def run_days(conn, asset_class: str, start: date, end: date, now: datetime, trading: Any = None) -> list[date]:
    """The run's days for this asset class whose fetch time has passed, oldest first."""
    out, d = [], start
    while d <= end and d <= now.date():
        if asset_class == "crypto":
            if cal.crypto_times(d).bars_fetch_at <= now:
                out.append(d)
        elif cal.session(d, conn, trading) is not None and cal.equity_times(d, conn, trading).bars_fetch_at <= now:
            out.append(d)
        d += timedelta(days=1)
    return out


@dataclass(frozen=True)
class Due:
    asset_class: str
    day: date
    kind: str            # 'run' (before the deadline) | 'settle_only' (after it)
    attempt: int         # this start's number


def due(conn, exp: ex.Experiment, now: datetime, trading: Any = None) -> tuple[list[Due], list[dict]]:
    """(what to run now, the days left alone and why), per the module doc."""
    todo, skipped = [], []
    end = exp.end_date or exp.start_date
    for ac in ASSETS:
        for d in run_days(conn, ac, exp.start_date, end, now, trading):
            if finished_ok(conn, ac, d):
                continue
            deadline = cal.decision_deadline(ac, d, conn, trading)
            starts = [t for _, t in _starts(conn, ac, d)]
            before = [t for t in starts if t <= deadline]
            after = [t for t in starts if t > deadline]
            if now <= deadline:
                if len(before) < MAX_ATTEMPTS:
                    todo.append(Due(ac, d, "run", len(starts) + 1))
                else:
                    skipped.append({"asset_class": ac, "date": d.isoformat(), "why": "attempts used",
                                    "attempts": len(before)})
            elif not after:
                todo.append(Due(ac, d, "settle_only", len(starts) + 1))
            else:
                skipped.append({"asset_class": ac, "date": d.isoformat(), "why": "gap: past the deadline",
                                "attempts": len(starts)})
    return todo, skipped


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------

@dataclass
class TickResult:
    status: str                              # 'idle' (nothing active) | 'busy' | 'ok' | 'error'
    ran: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if self.status == "error" else 0


def _tick_heartbeat(conn, run_id: str, now: datetime, detail: dict) -> None:
    with conn:
        conn.execute("INSERT INTO heartbeat (run_id, job, event, at, detail) VALUES (?, 'tick', ?, ?, ?)",
                     (run_id, "error" if detail.get("status") == "error" else "finished", db.utc_ts(now),
                      json.dumps(detail, sort_keys=True, default=str)))


def run_tick(data_dir: Path, now: Callable[[], datetime], make_deps: Callable[[], daily.Deps],
             trading: Any = None) -> TickResult:
    lock = open(data_dir / LOCK_NAME, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return TickResult("busy")
    try:
        return _tick(data_dir, now, make_deps, trading)
    finally:
        lock.close()


def _tick(data_dir, now, make_deps, trading) -> TickResult:
    active = read_active(data_dir)
    if active is None:
        return TickResult("idle")
    path = data_dir / f"{active.name}.db"
    if not path.exists():
        return TickResult("error", errors=[f"active experiment {active.name!r}: {path} not found"])
    res = TickResult("ok")
    tick_id = uuid.uuid4().hex
    conn = db.init_db(path)
    try:
        exp = ex.require(conn)
        if exp.name != active.name:
            raise TickError(f"{path} is labelled {exp.name!r}, not {active.name!r}")
        if active.no_ai and exp.name != DEV:
            raise TickError(f"--no-ai is only allowed on a dev label, not {exp.name!r}")
        todo, res.skipped = due(conn, exp, now(), trading)
        deps = make_deps() if todo else None
        for job in todo:
            try:
                held = daily.lock(path, job.asset_class)
            except daily.RunLocked as e:
                res.skipped.append({"asset_class": job.asset_class, "date": job.day.isoformat(), "why": str(e)})
                continue
            try:
                rep = daily.run_day(conn, job.asset_class, job.day, deps, ai=not active.no_ai)
            except Exception as e:  # noqa: BLE001 - logged by run_day; the tick reports it and goes on
                rep = None
                res.errors.append(f"{job.asset_class} {job.day}: {type(e).__name__}: {e}")
            finally:
                held.close()
            entry = {"asset_class": job.asset_class, "date": job.day.isoformat(), "kind": job.kind,
                     "attempt": job.attempt, "status": None if rep is None else rep.status,
                     "late": None if rep is None else rep.late}
            res.ran.append(entry)
            if rep is not None:
                print(daily.format_report(rep))
                if rep.status == "error":
                    res.errors.append(f"{job.asset_class} {job.day} ({job.kind}): {'; '.join(rep.errors)[:500]}")
    except Exception as e:  # noqa: BLE001
        res.errors.append(f"{type(e).__name__}: {e}")
        res.errors.append(traceback.format_exc()[-1500:])
    if res.errors:
        res.status = "error"
    try:
        _tick_heartbeat(conn, tick_id, now(), {"status": res.status, "experiment": active.name,
                                               "no_ai": active.no_ai, "ran": res.ran, "skipped": res.skipped,
                                               "errors": [e[:500] for e in res.errors]})
    finally:
        conn.close()
    return res


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run whatever is due now for the active experiment.")
    sub = p.add_subparsers(dest="cmd")
    a = sub.add_parser("activate", help="make a dev experiment active (never shakedown or official)")
    a.add_argument("name")
    a.add_argument("--no-ai", action="store_true", help="rule-based agents and C0 only ($0)")
    sub.add_parser("deactivate", help="empty data/active_experiment: ticks do nothing")
    args = p.parse_args(argv)
    data_dir = db.DATA_DIR
    try:
        if args.cmd == "activate":
            print(f"wrote {write_active(data_dir, args.name, args.no_ai)}")
            return 0
        if args.cmd == "deactivate":
            (data_dir / ACTIVE_NAME).write_text("", encoding="utf-8")
            print(f"emptied {data_dir / ACTIVE_NAME}")
            return 0
        res = run_tick(data_dir, lambda: datetime.now(timezone.utc), daily.real_deps)
    except (TickError, ex.ExperimentError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"tick {stamp}: {res.status}; ran {len(res.ran)}"
          + "".join(f"\n  ran {r['asset_class']} {r['date']} {r['kind']} #{r['attempt']}: {r['status']}" for r in res.ran)
          + "".join(f"\n  left {s['asset_class']} {s['date']}: {s['why']}" for s in res.skipped)
          + "".join(f"\n  ERROR {e}" for e in res.errors[:5]))
    return res.exit_code


if __name__ == "__main__":
    sys.exit(main())
