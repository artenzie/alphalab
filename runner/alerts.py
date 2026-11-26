"""Phone alerts via ntfy.sh, and the healthchecks.io dead-man's switch.

    python -m runner.alerts test                     # one message to the phone now
    python -m runner.alerts init-topic               # write NTFY_TOPIC to .env (once) and print it
    python -m runner.alerts check                    # what every tick does after its runs, now
    python -m runner.alerts notify --key K --title T [--priority 4] BODY   # used by deploy/backup.sh

What is sent (each queued in the experiment DB's append-only `alerts` table, sent from there):
  * problems, once each, plus one "resolved" when they recover (priority 4, resolved 2):
      error:<ac>:<date>   a daily_run ended in error, no ok run yet, the deadline still ahead;
                          or, past the deadline, STUCK_AFTER settle-only runs failed to mark the
                          day (later days of that asset class wait: correct it by hand), opened
                          once; resolved when the day finishes ok or missed
      missed:<ac>:<date>  the decision deadline passed without an ok run, opened once ever (never
                          re-opened after it is resolved); resolved when that day, or a later day
                          of the same asset class, runs ok
      tick                the last tick failed for a reason that is not a run's own error
      credit_low          OpenRouter credit left (total_credits - total_usage) under $5 (priority 3)
      freeze:<sym>:<ex>   a stock frozen by its corporate actions (§7 rule 11, v1.18): held at its
                          last close, orders in it rejected; resolved when the freeze lifts
  * notices, once per key:
      summary:us_equity:<date>  after each equity day is final (priority 2): status, late, AI cost,
                                every agent's equity and day return, the last crypto run, the last backup
      spend:<YYYY-MM>           the month's OpenRouter usage passed 80% of the key's limit, or of
                                OPENROUTER_MONTHLY_CAP when the key has none (priority 3)
      backup:<date>             a nightly backup failed (deploy/backup.sh, priority 4)
Problems look back LOOKBACK_DAYS only, so switching alerts on doesn't replay old gaps.

Alerts never block or fail a run: runner.tick calls after_tick() after its runs and swallows
anything it raises. A failed send stays pending (no alert_sends row) and is retried next tick.
No NTFY_TOPIC: nothing is sent (the queue still fills). No HEALTHCHECKS_URL: no pings.
Neither the topic nor any key is ever printed (the topic once, by init-topic).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import string
import sys
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from data import calendar as cal
from data import corporate_actions as CA
from data import db
from runner import daily
from runner import experiment as ex
from runner import status as st
from runner import tick

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
NTFY_URL = "https://ntfy.sh"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
TIMEOUT = 10                              # seconds, every network call
LOOKBACK_DAYS = 7
STUCK_AFTER = 2                           # failed settle-only runs past the deadline before error:
UNFINISHED_AFTER = timedelta(hours=3)     # a run with no finish row this old died (TimeoutStartSec)
SPEND_SHARE = 0.8
CREDIT_FLOOR = 5.0
PING_EVERY = 240                          # seconds between pings while a long tick runs
BACKUP_MARKER = Path.home() / ".cache" / "alphalab-backup" / "last.json"
BACKUP_STALE = timedelta(hours=36)

HIGH, NORMAL, LOW = 4, 3, 2               # ntfy priorities (5 urgent .. 1 min)
ASSET_NAME = {"us_equity": "equity", "crypto": "crypto"}

Sender = Callable[[str, str, int, str], None]       # (title, body, priority, tags); raises on failure


def load_env() -> None:
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH)


def log(msg: str) -> None:
    print(f"alerts: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

class SendError(Exception):
    pass


def ntfy_sender(topic: str, session: Any = requests) -> Sender:
    """Publish as JSON (UTF-8 titles are fine). Errors name the status only, never the topic."""
    def send(title: str, body: str, priority: int, tags: str = "") -> None:
        payload = {"topic": topic, "title": title, "message": body, "priority": priority}
        if tags:
            payload["tags"] = [t for t in tags.split(",") if t]
        try:
            r = session.post(NTFY_URL, json=payload, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise SendError(f"ntfy: {type(e).__name__}") from None
        if r.status_code >= 300:
            raise SendError(f"ntfy: HTTP {r.status_code}")
    return send


def env_sender() -> Sender | None:
    topic = os.getenv("NTFY_TOPIC", "").strip()
    return ntfy_sender(topic) if topic else None


# ---------------------------------------------------------------------------
# The queue (append-only: state is read from the rows, never updated)
# ---------------------------------------------------------------------------

def is_open(conn: sqlite3.Connection, key: str) -> bool:
    r = conn.execute("SELECT event FROM alerts WHERE key = ? AND event IN ('open', 'resolved') "
                     "ORDER BY alert_id DESC LIMIT 1", (key,)).fetchone()
    return r is not None and r[0] == "open"


def has_key(conn: sqlite3.Connection, key: str) -> bool:
    return conn.execute("SELECT 1 FROM alerts WHERE key = ? LIMIT 1", (key,)).fetchone() is not None


def queue(conn, key: str, event: str, title: str, body: str, priority: int, now: datetime) -> int:
    with conn:
        return conn.execute("INSERT INTO alerts (key, event, title, body, priority, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (key, event, title, body, priority, db.utc_ts(now))).lastrowid


def open_problem(conn, key, title, body, now, priority=HIGH) -> bool:
    if is_open(conn, key):
        return False
    queue(conn, key, "open", title, body, priority, now)
    return True


def resolve(conn, key, title, body, now) -> bool:
    if not is_open(conn, key):
        return False
    queue(conn, key, "resolved", title, body, LOW, now)
    return True


def notice_once(conn, key, title, body, now, priority) -> bool:
    if has_key(conn, key):
        return False
    queue(conn, key, "notice", title, body, priority, now)
    return True


def open_keys(conn, prefix: str) -> list[str]:
    keys = [r[0] for r in conn.execute("SELECT DISTINCT key FROM alerts WHERE key LIKE ? ESCAPE '\\'",
                                       (prefix.replace("_", "\\_") + "%",))]
    return sorted(k for k in keys if is_open(conn, k))


def pending(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT a.* FROM alerts AS a LEFT JOIN alert_sends AS s ON s.alert_id = a.alert_id "
                        "WHERE s.send_id IS NULL ORDER BY a.alert_id").fetchall()


_TAGS = {"open": "warning", "resolved": "white_check_mark", "notice": ""}


def flush(conn, send: Sender | None, now: Callable[[], datetime]) -> tuple[int, int]:
    """Send pending alerts oldest first; stop at the first failure (retried next tick). (sent, left)."""
    rows = pending(conn)
    if send is None:
        if rows:
            log(f"NTFY_TOPIC not set: {len(rows)} alert(s) left unsent")
        return 0, len(rows)
    sent = 0
    for r in rows:
        try:
            send(r["title"], r["body"], r["priority"], _TAGS.get(r["event"], ""))
        except Exception as e:  # noqa: BLE001 - a failed send is retried on the next tick
            log(f"send failed ({e}); {len(rows) - sent} alert(s) left for the next tick")
            return sent, len(rows) - sent
        with conn:
            conn.execute("INSERT INTO alert_sends (alert_id, sent_at) VALUES (?, ?)", (r["alert_id"], db.utc_ts(now())))
        sent += 1
    return sent, 0


# ---------------------------------------------------------------------------
# Problems: run errors, missed days, tick failures
# ---------------------------------------------------------------------------

def _runs(conn, ac: str, d: date, now: datetime) -> list[dict]:
    """status._runs, with a long-unfinished run counted as an error (a run in progress is not)."""
    out = []
    for r in st._runs(conn, ac, d):
        if r["status"] == "unfinished" and now - r["started"] > UNFINISHED_AFTER:
            r = {**r, "status": "error"}
        out.append(r)
    return out


def _first_error(conn, run_id: str) -> str:
    row = conn.execute("SELECT detail FROM heartbeat WHERE run_id = ? AND job = ? AND event = 'error' "
                       "ORDER BY heartbeat_id DESC LIMIT 1", (run_id, daily.JOB)).fetchone()
    d = json.loads(row[0]) if row and row[0] else {}
    errs = d.get("errors") or ([d["error"]] if d.get("error") else [])
    return (errs[0] if errs else "no error text").splitlines()[0][:300]


def _fmt_day(d: date) -> str:
    return f"{d:%a %d %b}"


def check_runs(conn, now: datetime, trading: Any = None) -> None:
    exp = ex.require(conn)
    start = max(exp.start_date, now.date() - timedelta(days=LOOKBACK_DAYS))
    end = exp.end_date or now.date()
    for ac in tick.ASSETS:
        name = ASSET_NAME[ac]
        last_ok: date | None = None
        for d in tick.run_days(conn, ac, start, end, now, trading):
            runs = _runs(conn, ac, d, now)
            ok = [r for r in runs if r["status"] == "ok"]
            missed = [r for r in runs if r["status"] == "missed"]
            err_key, miss_key = f"error:{ac}:{d}", f"missed:{ac}:{d}"
            if ok:
                last_ok = d
                late = " (late)" if ok[0]["late"] else ""
                resolve(conn, err_key, f"OK {name} {_fmt_day(d)}", f"{name} {d} ran ok{late} after the error.", now)
                resolve(conn, miss_key, f"OK {name} {_fmt_day(d)}", f"{name} {d} ran ok{late}.", now)
                continue
            deadline = cal.decision_deadline(ac, d, conn, trading)
            if now > deadline:
                if missed:
                    why = "held: every agent settled, marked and margin-checked (settle-only run)"
                elif runs:
                    why = f"{len(runs)} run(s), none ok yet; last error: {_first_error(conn, runs[-1]['run_id'])}"
                else:
                    why = "no run started at all"
                if not has_key(conn, miss_key):          # once ever: a resolved missed day stays resolved
                    queue(conn, miss_key, "open", f"MISSED {name} {_fmt_day(d)}",
                          f"{name} {d}: past its decision deadline ({deadline:%d %b %H:%M}Z) with no ok run. "
                          f"{why}. Nobody decides that day; runner.status shows it as missed.", HIGH, now)
                if missed:
                    resolve(conn, err_key, f"MARKED {name} {_fmt_day(d)}",
                            f"{name} {d}: every agent is marked now (missed day, held).", now)
                    continue
                failed = [r for r in runs if r["started"] > deadline and r["status"] == "error"]
                if len(failed) >= STUCK_AFTER and not _opened_since(conn, err_key, deadline):
                    queue(conn, err_key, "open", f"MARKS MISSING {name} {_fmt_day(d)}",
                          f"{name} {d}: {len(failed)} settle-only runs after the deadline failed to mark every "
                          f"agent; later {name} days wait for it. Last error: "
                          f"{_first_error(conn, failed[-1]['run_id'])}. Needs a manual correction.", HIGH, now)
            else:
                errs = [r for r in runs if r["status"] == "error"]
                if errs:
                    open_problem(conn, err_key, f"ERROR {name} {_fmt_day(d)}",
                                 f"{name} {d}: run {len(runs)} of {tick.MAX_ATTEMPTS} ended in error: "
                                 f"{_first_error(conn, errs[-1]['run_id'])}. The next tick retries "
                                 f"(deadline {deadline:%d %b %H:%M}Z).", now)
        if last_ok is not None:                   # a later ok day: the engine is back
            for key in open_keys(conn, f"missed:{ac}:"):
                gap = key.rsplit(":", 1)[1]
                if gap < last_ok.isoformat():
                    resolve(conn, key, f"RECOVERED {name}",
                            f"{name} {last_ok} ran ok; {gap} stays a gap.", now)


def _opened_since(conn, key: str, t: datetime) -> bool:
    return conn.execute("SELECT 1 FROM alerts WHERE key = ? AND event = 'open' AND created_at > ? LIMIT 1",
                        (key, db.utc_ts(t))).fetchone() is not None


def check_tick(conn, now: datetime) -> None:
    row = conn.execute("SELECT detail FROM heartbeat WHERE job = 'tick' ORDER BY heartbeat_id DESC LIMIT 1").fetchone()
    if row is None:
        return
    d = json.loads(row[0]) if row[0] else {}
    run_errors = sum(1 for r in d.get("ran", []) if r.get("status") == "error")
    errors = d.get("errors", [])
    if d.get("status") == "error" and len(errors) > run_errors:
        first = next((e for e in errors if e.strip()), "?").splitlines()[0][:300]
        open_problem(conn, "tick", "ERROR tick", f"The tick failed: {first}. "
                     "Check: journalctl -u alphalab-tick.service", now)
    elif d.get("status") == "ok":
        resolve(conn, "tick", "OK tick", "Ticks run cleanly again.", now)


# ---------------------------------------------------------------------------
# Daily summary
# ---------------------------------------------------------------------------

def read_backup_marker(path: Path = BACKUP_MARKER) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def backup_line(now: datetime, marker: dict | None) -> str:
    if not marker:
        return "backup: never ran (no marker)"
    try:
        at = cal._parse_ts(marker["finished_at"])
    except (KeyError, ValueError, TypeError):
        return "backup: marker unreadable"
    line = f"backup {marker.get('date', '?')}: {marker.get('status', '?')}"
    if marker.get("status") != "ok" and marker.get("step"):
        line += f" at {marker['step']}"
    if now - at > BACKUP_STALE:
        line += f" -- NONE SINCE ({(now - at).days}d {(now - at).seconds // 3600}h ago)"
    return line


def _equity_rows(conn, d: date) -> list[tuple[str, float, float | None]]:
    """(agent, equity on its latest day <= d, day return vs the row before), latest correction wins."""
    rows = conn.execute(
        """SELECT e.agent_id, e.as_of_date, e.equity FROM equity AS e
           WHERE e.as_of_date <= ? AND e.equity_id = (SELECT MAX(q.equity_id) FROM equity AS q
                 WHERE q.agent_id = e.agent_id AND q.as_of_date = e.as_of_date)
           ORDER BY e.agent_id, e.as_of_date""", (d.isoformat(),)).fetchall()
    by: dict[str, list[float]] = {}
    for aid, _, eq in rows:
        by.setdefault(aid, []).append(eq)
    out = []
    for aid in sorted(by, key=daily._order):
        v = by[aid]
        out.append((aid, v[-1], v[-1] / v[-2] - 1 if len(v) > 1 and v[-2] else None))
    return out


def _cost(conn, ac: str, d: date) -> tuple[float, int]:
    total, n = 0.0, 0
    for table in ("decisions", "shadow_decisions"):
        c, k = conn.execute(f"""SELECT COALESCE(SUM(x.cost_usd), 0), COUNT(x.cost_usd) FROM {table} AS x
                                JOIN snapshots AS s ON s.snapshot_id = x.snapshot_id
                                WHERE s.asset_class = ? AND s.snapshot_date = ?""", (ac, d.isoformat())).fetchone()
        total, n = total + c, n + k
    return total, n


def equity_final(conn, d: date, now: datetime, trading: Any = None) -> tuple[bool, dict | None]:
    """(is the equity day final, its ok run or None): a run finished ok, or one finished missed."""
    runs = _runs(conn, "us_equity", d, now)
    ok = next((r for r in runs if r["status"] == "ok"), None)
    if ok:
        return True, ok
    return any(r["status"] == "missed" for r in runs), None


def summary_text(conn, d: date, now: datetime, ok_run: dict | None, marker: dict | None,
                 trading: Any = None) -> str:
    if ok_run:
        state = "ok, late" if ok_run["late"] else "ok, on time"
        if ok_run["late"]:
            m = conn.execute("""SELECT json_extract(detail, '$.minutes_late') FROM heartbeat WHERE run_id = ?
                                AND job = ? AND event = 'finished'""", (ok_run["run_id"], daily.JOB)).fetchone()
            if m and m[0] is not None:
                state += f" by {m[0]} min"
    else:
        state = "MISSED (held: settled, marked, margin-checked after the deadline)"
    cost, calls = _cost(conn, "us_equity", d)
    lines = [f"equity {d}: {state}", f"AI cost ${cost:.4f} ({calls} priced calls)"]
    s = st.summary(conn, now, trading=trading)
    c = s.last_runs.get("crypto")
    lines.append("crypto: none yet" if c is None else
                 f"crypto {c['date']}: {c['status']}" + (" (late)" if c["late"] else ""))
    lines.append(backup_line(now, marker))
    lines += freeze_lines(conn, now)
    lines.append("")
    for aid, eq, ret in _equity_rows(conn, d):
        lines.append(f"{aid:>3}  ${eq:>11,.0f}  " + ("   -   " if ret is None else f"{ret:+7.2%}"))
    return "\n".join(lines)


def check_summary(conn, now: datetime, marker: dict | None, trading: Any = None) -> None:
    """Queue the summary of the latest final equity day, once."""
    exp = ex.require(conn)
    start = max(exp.start_date, now.date() - timedelta(days=LOOKBACK_DAYS))
    end = exp.end_date or now.date()
    for d in reversed(tick.run_days(conn, "us_equity", start, end, now, trading)):
        final, ok_run = equity_final(conn, d, now, trading)
        if not final:
            continue
        key = f"summary:us_equity:{d}"
        if not has_key(conn, key):
            notice_once(conn, key, f"AlphaLab {_fmt_day(d)}" + ("" if ok_run else " (missed)"),
                        summary_text(conn, d, now, ok_run, marker, trading), now, LOW)
        return


# ---------------------------------------------------------------------------
# OpenRouter spend and credit
# ---------------------------------------------------------------------------

def _scrub(text: str, key: str) -> str:
    return text.replace(key, "[redacted]") if key else text


def _get_json(session: Any, url: str, key: str) -> dict:
    try:
        r = session.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise SendError(f"{url.rsplit('/', 1)[1]}: {type(e).__name__}") from None
    if r.status_code != 200:
        raise SendError(f"{url.rsplit('/', 1)[1]}: HTTP {r.status_code}")
    try:
        return r.json().get("data") or {}
    except ValueError:
        raise SendError(f"{url.rsplit('/', 1)[1]}: not JSON") from None


def _float(x) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def check_spend(conn, now: datetime, session: Any = requests) -> None:
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        return
    try:
        data = _get_json(session, OPENROUTER_KEY_URL, key)
        limit = _float(data.get("limit"))
        source = "the key's limit"
        if limit is None:
            limit, source = _float(os.getenv("OPENROUTER_MONTHLY_CAP")), "OPENROUTER_MONTHLY_CAP"
        used = _float(data.get("usage_monthly"))
        if limit is None or limit <= 0:
            log("spend: the key has no limit and OPENROUTER_MONTHLY_CAP is not set; not checked")
        elif used is not None and used >= SPEND_SHARE * limit:
            notice_once(conn, f"spend:{now:%Y-%m}", f"OpenRouter spend {used / limit:.0%} of cap",
                        f"OpenRouter usage this month ${used:.2f} of ${limit:.2f} ({source}).", now, NORMAL)
    except Exception as e:  # noqa: BLE001
        log(f"spend check failed: {_scrub(str(e), key)}")
    try:
        data = _get_json(session, OPENROUTER_CREDITS_URL, key)
        total, usage = _float(data.get("total_credits")), _float(data.get("total_usage"))
        if total is None or usage is None:
            log("credits: no total_credits/total_usage in the reply; not checked")
            return
        left = total - usage
        if left < CREDIT_FLOOR:
            open_problem(conn, "credit_low", f"OpenRouter credit ${left:.2f} left",
                         f"OpenRouter credit left ${left:.2f} (bought ${total:.2f}, used ${usage:.2f}); "
                         f"under ${CREDIT_FLOOR:.0f}. Top up before AI calls fail.", now, NORMAL)
        else:
            resolve(conn, "credit_low", "OpenRouter credit ok", f"OpenRouter credit left ${left:.2f}.", now)
    except Exception as e:  # noqa: BLE001
        log(f"credit check failed: {_scrub(str(e), key)}")


# ---------------------------------------------------------------------------
# Healthchecks.io dead-man's switch
# ---------------------------------------------------------------------------

def ping(url: str | None = None, session: Any = requests) -> bool:
    url = (url if url is not None else os.getenv("HEALTHCHECKS_URL", "")).strip()
    if not url:
        return False
    try:
        session.get(url, timeout=TIMEOUT)
        return True
    except requests.RequestException as e:
        log(f"healthchecks ping failed: {type(e).__name__}")
        return False


class Pinger:
    """Ping now and then every PING_EVERY seconds until stopped, so a 3-hour tick still looks alive."""

    def __init__(self, every: float = PING_EVERY, ping_fn: Callable[[], Any] | None = None):
        self.every, self.ping_fn = every, ping_fn or (lambda: ping())
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while True:
            try:
                self.ping_fn()
            except Exception:  # noqa: BLE001
                pass
            if self._stop.wait(self.every):
                return

    def __enter__(self) -> "Pinger":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# After every tick
# ---------------------------------------------------------------------------

@dataclass
class Hooks:
    send: Sender | None = None
    session: Any = requests
    marker: Path = BACKUP_MARKER
    trading: Any = None


def check_freezes(conn, now: datetime) -> None:
    """One high-priority alert per frozen stock (v1.18), resolved once when its freeze lifts."""
    open_now = {}
    for f in CA.open_freezes(conn, now):
        key = f"freeze:{f['symbol']}:{f['ex_date']}"
        open_now[key] = f
        try:
            held = f" Held at ${CA.frozen_mark(conn, f['symbol'], f['ex_date'], now):,.2f} (its last close before)."
        except CA.CorporateActionError:
            held = ""
        open_problem(conn, key, f"{f['symbol']} frozen ({f['ex_date']})",
                     f"{f['reason']}.{held} No new orders in it; every other stock settles as usual. Lifts by itself "
                     f"on a confirming fetch, or: python -m data.corporate_actions resolve {f['symbol']} "
                     f"{f['ex_date']} --ratio R | --no-change --experiment <name>", now, HIGH)
    for key in open_keys(conn, "freeze:"):
        if key not in open_now:
            _, sym, ex_date = key.split(":", 2)
            e = CA.latest_event(conn, sym, ex_date)
            resolve(conn, key, f"{sym} unfrozen ({ex_date})",
                    f"{e['event']}: {e['reason']}" if e is not None else "no longer frozen", now)


def freeze_lines(conn, now: datetime) -> list[str]:
    return [f"FROZEN {f['symbol']} (ex {f['ex_date']}): {f['reason']}" for f in CA.open_freezes(conn, now)]


def run_checks(conn, t0: datetime, now: Callable[[], datetime], hooks: Hooks, spend: bool) -> tuple[int, int]:
    steps = [("runs", lambda: check_runs(conn, now(), hooks.trading)),
             ("freezes", lambda: check_freezes(conn, now())),
             ("tick", lambda: check_tick(conn, now())),
             ("summary", lambda: check_summary(conn, now(), read_backup_marker(hooks.marker), hooks.trading))]
    if spend:
        steps.append(("spend", lambda: check_spend(conn, now(), hooks.session)))
    for name, fn in steps:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - one failed check never stops the others or the send
            log(f"{name} check failed: {type(e).__name__}: {e}")
    return flush(conn, hooks.send, now)


def _active_conn(data_dir: Path) -> sqlite3.Connection | None:
    active = tick.read_active(data_dir)
    if active is None:
        return None
    path = data_dir / f"{active.name}.db"
    return db.init_db(path) if path.exists() else None


def after_tick(data_dir: Path, t0: datetime, now: Callable[[], datetime], hooks: Hooks | None = None,
               spend: bool | None = None) -> tuple[int, int]:
    """Checks and sends for the active experiment. Spend is checked on the hour's first tick."""
    hooks = hooks or Hooks(send=env_sender())
    conn = _active_conn(data_dir)
    if conn is None:
        return 0, 0
    try:
        return run_checks(conn, t0, now, hooks, t0.minute < 5 if spend is None else spend)
    finally:
        conn.close()


def notify(data_dir: Path, key: str, title: str, body: str, priority: int, now: Callable[[], datetime],
           send: Sender | None) -> str:
    """Queue a notice in the active experiment's DB and flush; no active DB: send directly, once."""
    conn = _active_conn(data_dir)
    if conn is None:
        if send is None:
            return "no active experiment and no NTFY_TOPIC: not sent"
        send(title, body, priority, "")
        return "sent directly (no active experiment to queue it in)"
    try:
        queued = notice_once(conn, key, title, body, now(), priority)
        sent, left = flush(conn, send, now)
        return f"{'queued' if queued else 'already queued'}; sent {sent}, left {left}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# .env topic and CLI
# ---------------------------------------------------------------------------

def init_topic(env_path: Path = ENV_PATH) -> str | None:
    """Append NTFY_TOPIC=alphalab-<20 random chars> to .env; None if it already has one."""
    from dotenv import dotenv_values
    if env_path.exists() and (dotenv_values(env_path).get("NTFY_TOPIC") or "").strip():
        return None
    topic = "alphalab-" + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(20))
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    with env_path.open("a", encoding="utf-8") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        f.write(f"NTFY_TOPIC={topic}\n")
    return topic


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phone alerts (ntfy.sh) and the healthchecks.io ping.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("test", help="send one test message now")
    sub.add_parser("init-topic", help="write a random NTFY_TOPIC to .env, once, and print it")
    sub.add_parser("check", help="run the after-tick checks and sends now (spend and credit too)")
    n = sub.add_parser("notify", help="queue a one-off notice (deduped on --key) and send it")
    n.add_argument("--key", required=True)
    n.add_argument("--title", required=True)
    n.add_argument("--priority", type=int, default=HIGH, choices=range(1, 6))
    n.add_argument("body")
    a = p.parse_args(argv)
    if a.cmd == "init-topic":
        topic = init_topic()
        if topic is None:
            print("NTFY_TOPIC is already set in .env; not changed (and not printed)")
            return 1
        print(f"wrote NTFY_TOPIC to {ENV_PATH}. Subscribe to this topic in the ntfy app (shown once):\n  {topic}")
        return 0
    load_env()
    send = env_sender()
    clock = lambda: datetime.now(timezone.utc)  # noqa: E731
    if a.cmd == "test":
        if send is None:
            print("ERROR: NTFY_TOPIC is not set in .env (run: python -m runner.alerts init-topic)", file=sys.stderr)
            return 1
        try:
            send("AlphaLab test", f"Test message from the Pi at {clock():%Y-%m-%d %H:%M:%S}Z.", NORMAL, "test_tube")
        except SendError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print("sent")
        return 0
    if a.cmd == "check":
        sent, left = after_tick(db.DATA_DIR, clock(), clock, Hooks(send=send), spend=True)
        print(f"sent {sent}, left {left}")
        return 0 if left == 0 else 1
    try:
        print(notify(db.DATA_DIR, a.key, a.title, a.body, a.priority, clock, send))
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
