"""A plain, read-only status page for the Pi, served on the Tailscale address only.

    python -m dashboard.status_page                  # http://<tailscale0 IPv4>:8080/
    python -m dashboard.status_page --port 8081

Shows what runner.status prints, plus each agent's latest equity and return since start, the
last decision per AI agent, today's and the month's AI cost (recorded in the DB), the last
backup, and the open alerts. The experiment is the one named in data/active_experiment, read
again on every request.

Read-only by construction: every request opens the DB with SQLite mode=ro and closes it, and
the trading calendar is read from the DB's cache only (a day not cached shows as an error;
the page never fetches from Alpaca or writes the cache). No API keys are read.

Binds to the tailscale0 IPv4 address and nothing else: if tailscale0 has no address it exits 1
(systemd restarts it); 0.0.0.0, :: and other wildcard hosts are refused.
"""

from __future__ import annotations

import argparse
import fcntl
import html
import ipaddress
import socket
import sqlite3
import struct
import sys
import traceback
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from data import db
from execution.ledger import START_CASH
from runner import alerts, daily, status, tick

PORT = 8080
INTERFACE = "tailscale0"
REFRESH_S = 60
SIOCGIFADDR = 0x8915
ERROR_CHARS = 200


class PageError(Exception):
    pass


class CacheOnlyCalendar:
    """Stands in for the Alpaca client: a day missing from the calendar cache raises, never fetches."""

    def get_calendar(self, req: Any) -> Any:
        raise PageError("the status page reads the cached calendar only (no Alpaca fetch)")


# ---------------------------------------------------------------------------
# Data (pure reads on a read-only connection)
# ---------------------------------------------------------------------------

def open_ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise PageError(f"{path} not found")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def equity_rows(conn: sqlite3.Connection) -> list[dict]:
    """Every agent with its latest equity row (latest correction wins) and return since START_CASH."""
    agents = conn.execute("SELECT agent_id, name, kind FROM agents").fetchall()
    out = []
    for a in sorted(agents, key=lambda r: daily._order(r["agent_id"])):
        r = conn.execute("""SELECT as_of_date, equity FROM equity WHERE agent_id = ?
                            ORDER BY as_of_date DESC, equity_id DESC LIMIT 1""", (a["agent_id"],)).fetchone()
        out.append({"agent_id": a["agent_id"], "name": a["name"], "kind": a["kind"],
                    "as_of": r["as_of_date"] if r else None, "equity": r["equity"] if r else None,
                    "return": r["equity"] / START_CASH - 1 if r else None})
    return out


def last_decisions(conn: sqlite3.Connection) -> list[dict]:
    """The latest decisions row (any attempt or correction) per AI agent."""
    agents = conn.execute("SELECT agent_id, name FROM agents WHERE kind = 'ai'").fetchall()
    out = []
    for a in sorted(agents, key=lambda r: daily._order(r["agent_id"])):
        r = conn.execute("""SELECT d.decided_at, d.status, d.attempt, d.error, s.snapshot_date
                            FROM decisions AS d JOIN snapshots AS s ON s.snapshot_id = d.snapshot_id
                            WHERE d.agent_id = ? ORDER BY d.decision_id DESC LIMIT 1""",
                         (a["agent_id"],)).fetchone()
        err = (r["error"] or "") if r else ""
        out.append({"agent_id": a["agent_id"], "name": a["name"],
                    "date": r["snapshot_date"] if r else None, "decided_at": r["decided_at"] if r else None,
                    "status": r["status"] if r else None, "attempt": r["attempt"] if r else None,
                    "error": err[:ERROR_CHARS] + ("..." if len(err) > ERROR_CHARS else "")})
    return out


def ai_cost(conn: sqlite3.Connection, lo: date, hi: date) -> tuple[float, int]:
    """(USD, priced calls) over decisions and shadow decisions with snapshot dates in [lo, hi]."""
    total, n = 0.0, 0
    for table in ("decisions", "shadow_decisions"):
        c, k = conn.execute(f"""SELECT COALESCE(SUM(x.cost_usd), 0), COUNT(x.cost_usd) FROM {table} AS x
                                JOIN snapshots AS s ON s.snapshot_id = x.snapshot_id
                                WHERE s.snapshot_date BETWEEN ? AND ?""",
                            (lo.isoformat(), hi.isoformat())).fetchone()
        total, n = total + c, n + k
    return total, n


def open_alerts(conn: sqlite3.Connection) -> list[dict]:
    """Keys whose last open/resolved row is 'open', with that row's title, time and priority."""
    rows = conn.execute("""SELECT a.key, a.title, a.created_at, a.priority FROM alerts AS a
                           WHERE a.alert_id = (SELECT MAX(b.alert_id) FROM alerts AS b
                                               WHERE b.key = a.key AND b.event IN ('open', 'resolved'))
                             AND a.event = 'open' ORDER BY a.alert_id""").fetchall()
    return [dict(r) for r in rows]


def page_data(conn: sqlite3.Connection, now: datetime, marker: dict | None,
              experiment: str = "?", no_ai: bool = False, trading: Any = None) -> dict:
    """Everything the page shows. A section that fails holds its error instead of failing the page."""
    trading = trading if trading is not None else CacheOnlyCalendar()
    today = now.date()
    out: dict[str, Any] = {"now": now, "experiment": experiment, "no_ai": no_ai}
    sections: dict[str, Callable[[], Any]] = {
        "status": lambda: status.summary(conn, now, trading=trading),
        "equity": lambda: equity_rows(conn),
        "decisions": lambda: last_decisions(conn),
        "cost_today": lambda: ai_cost(conn, today, today),
        "cost_month": lambda: ai_cost(conn, today.replace(day=1), today),
        "alerts": lambda: open_alerts(conn),
    }
    out["errors"] = {}
    for name, fn in sections.items():
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001 - shown on the page
            out[name] = None
            out["errors"][name] = f"{type(e).__name__}: {e}"
    out["backup"] = alerts.backup_line(now, marker)
    return out


def load(data_dir: Path, now: datetime, marker_path: Path = alerts.BACKUP_MARKER,
         trading: Any = None) -> dict:
    active = tick.read_active(data_dir)
    if active is None:
        raise PageError(f"no active experiment ({data_dir / tick.ACTIVE_NAME} is missing or empty)")
    conn = open_ro(data_dir / f"{active.name}.db")
    try:
        return page_data(conn, now, alerts.read_backup_marker(marker_path), active.name, active.no_ai, trading)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def _e(x: Any) -> str:
    return html.escape("-" if x is None else str(x))


def _table(head: list[str], rows: list[list[Any]]) -> str:
    th = "".join(f"<th>{_e(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{_e(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table border=\"1\" cellpadding=\"4\"><tr>{th}</tr>{body}</table>"


def _err(d: dict, name: str) -> str | None:
    e = d["errors"].get(name)
    return None if e is None else f"<p><b>error:</b> {_e(e)}</p>"


def render(d: dict) -> str:
    now = d["now"]
    parts = [f"<h1>AlphaLab status</h1>",
             f"<p>experiment <b>{_e(d['experiment'])}</b>" + (" (--no-ai)" if d["no_ai"] else "")
             + f"; page made {_e(f'{now:%Y-%m-%d %H:%M:%S}Z')}; refreshes every {REFRESH_S} s</p>"]

    parts.append("<h2>Engine (runner.status)</h2>")
    parts.append(_err(d, "status") or f"<pre>{_e(status.format_status(d['status']))}</pre>")

    parts.append("<h2>Open alerts</h2>")
    a = d["alerts"]
    parts.append(_err(d, "alerts") or ("<p>none</p>" if not a else
                 _table(["since", "key", "title", "priority"],
                        [[x["created_at"], x["key"], x["title"], x["priority"]] for x in a])))

    parts.append("<h2>Backup</h2>")
    parts.append(f"<p>{_e(d['backup'])}</p>")

    parts.append("<h2>AI cost (recorded in the DB)</h2>")
    lines = []
    for key, label in (("cost_today", f"today ({now:%Y-%m-%d})"), ("cost_month", f"month to date ({now:%Y-%m})")):
        v = d[key]
        lines.append(f"<li>{_e(label)}: " + (_e(d['errors'][key]) if v is None else f"${v[0]:.4f} ({v[1]} priced calls)")
                     + "</li>")
    parts.append("<ul>" + "".join(lines) + "</ul>")

    parts.append(f"<h2>Equity (start ${START_CASH:,.0f})</h2>")
    eq = d["equity"]
    parts.append(_err(d, "equity") or _table(
        ["agent", "name", "kind", "as of", "equity", "return"],
        [[x["agent_id"], x["name"], x["kind"], x["as_of"],
          None if x["equity"] is None else f"${x['equity']:,.2f}",
          None if x["return"] is None else f"{x['return']:+.2%}"] for x in eq]))

    parts.append("<h2>Last decision per AI agent</h2>")
    ds = d["decisions"]
    parts.append(_err(d, "decisions") or _table(
        ["agent", "name", "date", "decided at", "status", "attempt", "error"],
        [[x["agent_id"], x["name"], x["date"], x["decided_at"], x["status"], x["attempt"], x["error"] or None]
         for x in ds]))

    body = "\n".join(parts)
    return ("<!doctype html>\n<html><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<meta http-equiv=\"refresh\" content=\"{REFRESH_S}\"><title>AlphaLab status</title></head>"
            f"<body>\n{body}\n</body></html>\n")


def error_page(msg: str) -> str:
    return (f"<!doctype html>\n<html><head><meta charset=\"utf-8\"><meta http-equiv=\"refresh\" content=\"{REFRESH_S}\">"
            f"<title>AlphaLab status: error</title></head><body><h1>AlphaLab status: error</h1>"
            f"<pre>{_e(msg)}</pre></body></html>\n")


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def interface_ipv4(name: str = INTERFACE) -> str | None:
    """The IPv4 address on a Linux interface, or None if it has none (or doesn't exist)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        res = fcntl.ioctl(s.fileno(), SIOCGIFADDR, struct.pack("256s", name.encode()[:15]))
        return socket.inet_ntoa(res[20:24])
    except OSError:
        return None
    finally:
        s.close()


def check_host(host: str) -> str:
    """Refuse wildcard and non-literal hosts: the page binds to one address only."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as e:
        raise PageError(f"bind host must be an IP address, not {host!r}") from e
    if ip.is_unspecified:
        raise PageError(f"refusing to bind to {host} (all interfaces)")
    return str(ip)


def make_handler(data_dir: Path, marker_path: Path, clock: Callable[[], datetime],
                 trading: Any = None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "alphalab-status"
        sys_version = ""

        def _send(self, code: int, text: str, body: bool = True) -> None:
            data = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if body:
                self.wfile.write(data)

        def _page(self, body: bool) -> None:
            if self.path.split("?", 1)[0] != "/":
                self._send(404, error_page("not found"), body)
                return
            try:
                text = render(load(data_dir, clock(), marker_path, trading))
            except Exception as e:  # noqa: BLE001 - shown, the server keeps running
                traceback.print_exc()
                self._send(500, error_page(f"{type(e).__name__}: {e}"), body)
                return
            self._send(200, text, body)

        def do_GET(self) -> None:  # noqa: N802
            self._page(True)

        def do_HEAD(self) -> None:  # noqa: N802
            self._page(False)

        def _no(self) -> None:
            self._send(405, error_page("read-only page: GET only"))

        do_POST = do_PUT = do_DELETE = do_PATCH = _no  # noqa: N815

    return Handler


def make_server(host: str, port: int, data_dir: Path = db.DATA_DIR, marker_path: Path = alerts.BACKUP_MARKER,
                clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                trading: Any = None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((check_host(host), port), make_handler(data_dir, marker_path, clock, trading))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Read-only status page on the Tailscale address.")
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--interface", default=INTERFACE)
    a = p.parse_args(argv)
    host = interface_ipv4(a.interface)
    if host is None:
        print(f"ERROR: {a.interface} has no IPv4 address (is tailscaled up?); not binding anywhere else",
              file=sys.stderr)
        return 1
    try:
        srv = make_server(host, a.port)
    except (PageError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"serving http://{host}:{a.port}/ (read-only)", flush=True)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
