"""Engine status and uptime from an experiment database (D2's Gate 2 rule).

    python -m runner.status                      # the active experiment (data/active_experiment)
    python -m runner.status --experiment dev
    python -m runner.status --db /path/to/x.db --since 2026-12-14

Prints the last run per asset class, errors in the last 24 h, every scheduled run with its
outcome, uptime, and gaps between ticks.

Scheduled runs: every day of the run whose protocol fetch time has passed (NYSE sessions for
equity, every calendar day for crypto; the same days runner.tick runs). Each is
  ok        a daily_run finished with status ok, on time
  late      finished ok from a late snapshot (v1.13)
  error     runs started, none finished ok
  missing   no run started at all
  pending   not finished ok yet, and its decision deadline hasn't passed (left out of uptime)
Uptime (D2: 99% or more keeps the engine on the Pi) = (ok + late) / (ok + late + error + missing).
The on-time share is ok / the same total.

Tick gaps: intervals over GAP_MIN minutes from the end of one tick to the start of the next (and
from the last tick's end to now), which measure the Pi being off or the timer not firing. A tick
that runs long (an on-time equity run waits ~30 min) is not a gap: systemd starts no other tick
meanwhile, and the tick's heartbeat carries its finish time.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from data import calendar as cal
from data import db
from runner import daily
from runner import experiment as ex
from runner import tick

GAP_MIN = 10


@dataclass
class Status:
    experiment: ex.Experiment
    now: datetime
    last_runs: dict[str, dict] = field(default_factory=dict)
    errors_24h: dict[str, int] = field(default_factory=dict)
    days: list[dict] = field(default_factory=list)
    ticks: int = 0
    first_tick: datetime | None = None
    last_tick: datetime | None = None
    gaps: list[tuple[datetime, datetime]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in ("ok", "late", "error", "missing", "pending")}
        for d in self.days:
            out[d["outcome"]] += 1
        return out

    @property
    def scheduled(self) -> int:
        c = self.counts()
        return c["ok"] + c["late"] + c["error"] + c["missing"]

    @property
    def uptime(self) -> float | None:
        c = self.counts()
        return (c["ok"] + c["late"]) / self.scheduled if self.scheduled else None

    @property
    def on_time(self) -> float | None:
        return self.counts()["ok"] / self.scheduled if self.scheduled else None


def _runs(conn, asset_class: str, day: date) -> list[dict]:
    out = []
    for run_id, at in tick._starts(conn, asset_class, day):
        fin = conn.execute("SELECT event, detail FROM heartbeat WHERE run_id = ? AND job = ? AND event IN "
                           "('finished', 'error') ORDER BY heartbeat_id DESC LIMIT 1", (run_id, daily.JOB)).fetchone()
        d = json.loads(fin[1]) if fin and fin[1] else {}
        out.append({"run_id": run_id, "started": at, "status": d.get("status") or (fin[0] if fin else "unfinished"),
                    "late": bool(d.get("late"))})
    return out


def summary(conn: sqlite3.Connection, now: datetime, since: date | None = None, trading: Any = None) -> Status:
    exp = ex.require(conn)
    st = Status(exp, now)
    start = max(exp.start_date, since) if since else exp.start_date
    end = exp.end_date or now.date()
    for ac in tick.ASSETS:
        last = conn.execute(
            """SELECT run_id, at, json_extract(detail, '$.date') FROM heartbeat WHERE job = ? AND event = 'started'
                 AND json_extract(detail, '$.asset_class') = ? ORDER BY at DESC, heartbeat_id DESC LIMIT 1""",
            (daily.JOB, ac)).fetchone()
        if last:
            runs = _runs(conn, ac, date.fromisoformat(last[2]))
            r = next(x for x in runs if x["run_id"] == last[0])
            st.last_runs[ac] = {"date": last[2], "started": cal._parse_ts(last[1]), "status": r["status"], "late": r["late"]}
        for d in tick.run_days(conn, ac, start, end, now, trading):
            runs = _runs(conn, ac, d)
            ok = [r for r in runs if r["status"] == "ok"]
            if ok:
                outcome = "late" if ok[0]["late"] else "ok"
            elif now <= cal.decision_deadline(ac, d, conn, trading):
                outcome = "pending"
            else:
                outcome = "error" if runs else "missing"
            st.days.append({"asset_class": ac, "date": d.isoformat(), "outcome": outcome, "attempts": len(runs)})
    since_ts = db.utc_ts(now - timedelta(hours=24))
    for job, n in conn.execute("SELECT job, COUNT(*) FROM heartbeat WHERE event = 'error' AND at >= ? GROUP BY job "
                               "ORDER BY job", (since_ts,)):
        st.errors_24h[job] = n
    lo = db.utc_ts(datetime(start.year, start.month, start.day, tzinfo=timezone.utc)) if since else ""
    ticks = []                                    # (started, finished): a long run is not downtime
    for at, detail in conn.execute("SELECT at, detail FROM heartbeat WHERE job = 'tick' AND at >= ? ORDER BY at",
                                   (lo,)):
        t0 = cal._parse_ts(at)
        fin = (json.loads(detail) if detail else {}).get("finished_at")
        ticks.append((t0, max(t0, cal._parse_ts(fin)) if fin else t0))
    st.ticks = len(ticks)
    if ticks:
        st.first_tick, st.last_tick = ticks[0][0], ticks[-1][0]
        limit = timedelta(minutes=GAP_MIN)
        alive = ticks[0][1]
        for t0, t1 in ticks[1:]:
            if t0 - alive > limit:
                st.gaps.append((alive, t0))
            alive = max(alive, t1)
        if now - alive > limit:
            st.gaps.append((alive, now))
    return st


def _fmt(t: datetime | None) -> str:
    return "-" if t is None else f"{t:%Y-%m-%d %H:%M}Z"


def _dur(td: timedelta) -> str:
    m = int(td.total_seconds() // 60)
    return f"{m // 1440}d {m % 1440 // 60}h {m % 60:02d}m" if m >= 1440 else f"{m // 60}h {m % 60:02d}m"


def format_status(st: Status, verbose: bool = False) -> str:
    e = st.experiment
    lines = [f"experiment {e.name!r}, {e.start_date} to {e.end_date}; now {_fmt(st.now)}", "", "Last run:"]
    for ac in tick.ASSETS:
        r = st.last_runs.get(ac)
        lines.append(f"  {ac:<9} " + ("none yet" if r is None else
                                      f"{r['date']} started {_fmt(r['started'])}: {r['status']}"
                                      + (" (late)" if r["late"] else "")))
    lines += ["", "Errors in the last 24 h: " + (", ".join(f"{j} x{n}" for j, n in st.errors_24h.items()) or "none")]
    c = st.counts()
    lines += ["", f"Scheduled runs: {st.scheduled} (ok {c['ok']}, late {c['late']}, error {c['error']}, "
                  f"missing {c['missing']}; pending {c['pending']})"]
    if st.uptime is not None:
        lines.append(f"Uptime (D2: >= 99% keeps the engine on the Pi): {st.uptime:.1%}; on time {st.on_time:.1%}")
    bad = [d for d in st.days if d["outcome"] not in ("ok",)]
    for d in (st.days if verbose else bad):
        lines.append(f"  {d['asset_class']:<9} {d['date']}  {d['outcome']}"
                     + (f" ({d['attempts']} attempts)" if d["attempts"] > 1 else ""))
    lines += ["", f"Ticks: {st.ticks}, first {_fmt(st.first_tick)}, last {_fmt(st.last_tick)}"]
    if st.gaps:
        total = sum((b - a for a, b in st.gaps), timedelta())
        lines.append(f"Gaps over {GAP_MIN} min: {len(st.gaps)}, {_dur(total)} in all")
        for a, b in st.gaps:
            lines.append(f"  {_fmt(a)} -> {_fmt(b)}  {_dur(b - a)}")
    else:
        lines.append(f"Gaps over {GAP_MIN} min: none")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Engine status and uptime (D2).")
    where = p.add_mutually_exclusive_group()
    where.add_argument("--experiment")
    where.add_argument("--db", type=Path)
    p.add_argument("--since", type=date.fromisoformat, default=None, metavar="YYYY-MM-DD")
    p.add_argument("--all", action="store_true", help="list every scheduled run, not only the ones not ok")
    a = p.parse_args(argv)
    try:
        if a.db:
            path = a.db
        else:
            name = a.experiment
            if name is None:
                active = tick.read_active(db.DATA_DIR)
                if active is None:
                    print("no active experiment (data/active_experiment is empty); use --experiment", file=sys.stderr)
                    return 1
                name = active.name
            path = ex.path_for(name)
        if not path.exists():
            print(f"ERROR: {path} not found", file=sys.stderr)
            return 1
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            st = summary(conn, datetime.now(timezone.utc), a.since)
        finally:
            conn.close()
    except (ex.ExperimentError, tick.TickError, cal.CalendarError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(format_status(st, a.all))
    return 0


if __name__ == "__main__":
    sys.exit(main())
