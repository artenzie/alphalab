"""The daily runner (runner/daily.py) end to end on a temp database, no network.

A synthetic history of the 23 universe symbols and the 5 coins is seeded, as
the ingest stores it. The run's outside world is faked: a clock whose sleep
advances time, an ingest that stores day D's bars at the moment it runs, a
fill-bar fetch, easy-to-borrow flags, and one scripted LLM client per AI agent.
"""

import json
import math
import sys
import threading
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from agents.llm import LLMResponse, LLMUnavailable  # noqa: E402
from data import calendar as cal  # noqa: E402
from data import db  # noqa: E402
from data.ingest import CRYPTO_SYMBOLS, load_universe  # noqa: E402
from execution import ledger as L  # noqa: E402
from execution import ledger_db as LD  # noqa: E402
from runner import daily  # noqa: E402
from runner import experiment as ex  # noqa: E402
from runner import roster  # noqa: E402
from test_snapshot import close_of, seed_calendar, sessions  # noqa: E402

UTC = timezone.utc
UNIVERSE = load_universe()
STOCKS = [s for s in UNIVERSE if s != "SPY"]
START = date(2026, 11, 16)                       # Mon
END = date(2026, 11, 24)                         # the dev label's last decision day (v1.15)
DAYS = [date(2026, 11, d) for d in (16, 17, 18, 19, 20, 23)]   # across a weekend
EQUITY_IDS = roster.EQUITY_RULE + roster.EQUITY_AI
LEDGER_TABLES = ("snapshots", "decisions", "risk_events", "orders", "fills", "positions", "equity")


# ---------------------------------------------------------------------------
# Synthetic market data, stored as the ingest stores it
# ---------------------------------------------------------------------------

def price(sym: str, i: int) -> float:
    h = sum(map(ord, sym)) % 17
    return (50 + 10 * h) * (1 + 0.08 * math.sin(i / 6 + h) + 0.0007 * i * (1 if h % 2 else -1))


def bar_row(sym, d, close, fetched_at, *, high=None):
    crypto = "/" in sym
    bar_ts = datetime(d.year, d.month, d.day, tzinfo=UTC) if crypto else cal.et_to_utc(d, dtime(0))
    hi = close * 1.01 if high is None else high
    raw = dict(open=close * 0.995, high=hi, low=close * 0.99, close=close, volume=1e6)
    return {"symbol": sym, "asset_class": "crypto" if crypto else "us_equity", "bar_date": d.isoformat(),
            "bar_ts": db.utc_ts(bar_ts), "feed": "crypto" if crypto else "iex",
            "fetched_at": db.utc_ts(fetched_at), **raw,
            **{f"adj_{k}": raw[k] for k in ("open", "high", "low", "close", "volume")}}


def insert_rows(conn, rows):
    if not rows:
        return
    cols = list(rows[0])
    with conn:
        conn.executemany(f"INSERT INTO prices ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [[r[c] for c in cols] for r in rows])


EQ_HIST = sessions(date(2026, 3, 2), date(2026, 12, 31))
EQ_INDEX = {d: i for i, d in enumerate(EQ_HIST)}
CR_FIRST = date(2026, 5, 1)


def eq_close(sym, d):
    return price(sym, EQ_INDEX[d])


def cr_close(sym, d):
    return price(sym, (d - CR_FIRST).days) * (1000 if sym == "BTC/USD" else 1)


def seed_history(conn, eq_last=date(2026, 11, 13), cr_last=date(2026, 11, 15)):
    rows = [bar_row(s, d, eq_close(s, d), close_of(d) + cal.BARS_FETCH_AFTER_CLOSE)
            for d in EQ_HIST if d <= eq_last for s in UNIVERSE]
    d = CR_FIRST
    while d <= cr_last:
        rows += [bar_row(s, d, cr_close(s, d), cal.crypto_times(d).bars_fetch_at) for s in CRYPTO_SYMBOLS]
        d += timedelta(days=1)
    insert_rows(conn, rows)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


class Market:
    """The fake ingest and fill-bar fetch: day D's bars appear when the ingest runs."""

    def __init__(self, clock, bad=None, missing=()):
        self.clock, self.bad, self.missing = clock, bad or {}, set(missing)
        self.ingests, self.fill_fetches, self.window_starts = [], [], []
        self.no_trades: set[str] = set()

    def ingest(self, conn, asset_class, run_id, now):
        t = now()
        self.ingests.append((asset_class, t))
        with conn:                                         # the real ingest's heartbeat, same run_id
            conn.execute("INSERT INTO heartbeat (run_id, job, event, at, detail) VALUES (?, 'fetch_prices', "
                         "'started', ?, '{}')", (run_id, db.utc_ts(t)))
        if asset_class == "us_equity":
            d = max(d for d in EQ_HIST if close_of(d) <= t)
            have = {r[0] for r in conn.execute("SELECT symbol FROM prices WHERE bar_date = ?", (d.isoformat(),))}
            rows = []
            for s in UNIVERSE:
                if s in have or (s, d) in self.missing:
                    continue
                c = eq_close(s, d)
                rows.append(bar_row(s, d, c, t, high=c * 0.9 if (s, d) in self.bad else None))
        else:
            d = (t - timedelta(minutes=1)).date() - timedelta(days=1)
            have = {r[0] for r in conn.execute("SELECT symbol FROM prices WHERE bar_date = ?", (d.isoformat(),))}
            rows = [bar_row(s, d, cr_close(s, d), t) for s in CRYPTO_SYMBOLS if s not in have]
        insert_rows(conn, rows)
        self.clock.sleep(60)                               # an ingest takes a minute
        with conn:
            conn.execute("INSERT INTO heartbeat (run_id, job, event, at, detail) VALUES (?, 'fetch_prices', "
                         "'finished', ?, ?)", (run_id, db.utc_ts(now()), json.dumps({"inserted": len(rows)})))
        return 0

    def fill_prices(self, conn, dates, run_id, now, window_starts=None):
        for d in dates:
            self.fill_fetches.append(d)
            w0 = (window_starts or {}).get(d) or cal.crypto_times(d).decide_at
            self.window_starts.append(w0)
            fetched = db.utc_ts(now())
            with conn:
                if now() >= w0 + timedelta(hours=1):
                    conn.execute("INSERT INTO crypto_fill_windows (decision_date, window_start, window_end, symbols, "
                                 "fetched_at, run_id) VALUES (?, ?, ?, ?, ?, ?)",
                                 (d.isoformat(), db.utc_ts(w0), db.utc_ts(w0 + timedelta(hours=1)),
                                  json.dumps(sorted(CRYPTO_SYMBOLS)), fetched, run_id))
                for s in CRYPTO_SYMBOLS:
                    if s in self.no_trades:
                        continue
                    base = cr_close(s, d + timedelta(days=1)) if d + timedelta(days=1) >= CR_FIRST else 1.0
                    for m in range(60):
                        o = base * (1 + 0.0001 * m)
                        conn.execute(
                            """INSERT OR IGNORE INTO crypto_fill_bars (symbol, decision_date, window_start, bar_ts,
                               open, high, low, close, volume, feed, fetched_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'crypto', ?)""",
                            (s, d.isoformat(), db.utc_ts(w0), db.utc_ts(w0 + timedelta(minutes=m)), o, o * 1.001,
                             o * 0.999, o, fetched))
        return 0


def reply(model, targets, text=None):
    body = text or json.dumps({"action": "rebalance", "targets": targets,
                               "reasons": {s: f"reason for {s}" for s in targets},
                               "reasoning": "synthetic test reasoning", "confidence": 0.6})
    return LLMResponse(body, model.model_id, model.provider_names[0], 1000, 200, 0.001, 50, 0, "stop")


AI_TARGETS = {
    "8": {"AAPL": 0.15, "NVDA": 0.15, "XOM": -0.1},
    "9": {"JPM": 0.2, "LLY": 0.2},
    "10": {"META": 0.1, "GOOGL": 0.1, "TSLA": -0.05},
    "11": {"COST": 0.2, "WMT": 0.2, "CVX": 0.2},
    "12": {"AMZN": 0.3},                          # clipped to 20%
    "C1": {"BTC/USD": 0.6, "ETH/USD": 0.3},       # BTC clipped to 50%
}


class Client:
    """A scripted LLM client: replies with the agent's targets; can fail, or wait on a barrier."""

    def __init__(self, agent_id, fail=False, barrier=None):
        self.agent_id, self.fail, self.barrier = agent_id, fail, barrier
        self.calls = []

    def complete(self, prompt, *, model):
        self.calls.append(prompt)
        if self.barrier is not None and len(self.calls) == 1:
            self.barrier.wait(timeout=10)          # every AI agent must be in flight at once
        if self.fail:
            raise LLMUnavailable("timeout: read timed out after 300 s")
        return reply(model, AI_TARGETS[self.agent_id])


def make_deps(clock, market, clients):
    return daily.Deps(now=clock, sleep=clock.sleep, ingest=market.ingest, fill_prices=market.fill_prices,
                      etb=lambda syms: {s: True for s in syms}, client_for=lambda aid: clients[aid],
                      trading=no_network, ai_sleep=lambda s: None, ai_backoff=(300, 600, 900))


def no_network():
    raise RuntimeError("no network in tests")


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "run.db")
    seed_calendar(c)
    seed_history(c)
    with c:
        c.execute("INSERT INTO experiment (name, start_date, end_date) VALUES ('dev', ?, ?)",
                  (START.isoformat(), END.isoformat()))
    yield c
    c.close()


@pytest.fixture
def clients():
    return {aid: Client(aid) for aid in roster.EQUITY_AI + roster.CRYPTO_AI}


def equity_run(conn, d, clients, market=None, at=None):
    clock = Clock(at or cal.equity_times(d, conn).bars_fetch_at)
    market = market or Market(clock)
    market.clock = clock
    return daily.run_day(conn, "us_equity", d, make_deps(clock, market, clients)), clock


def counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in LEDGER_TABLES}


def heartbeat(conn, run_id):
    return [(r["job"], r["event"], json.loads(r["detail"]) if r["detail"] else None)
            for r in conn.execute("SELECT * FROM heartbeat WHERE run_id = ? ORDER BY heartbeat_id", (run_id,))]


def n_calls(clients):
    return sum(len(c.calls) for c in clients.values())


# ---------------------------------------------------------------------------
# 1. A full week, end to end
# ---------------------------------------------------------------------------

def test_equity_end_to_end_over_several_days(conn, clients, capsys):
    reports = [equity_run(conn, d, clients)[0] for d in DAYS]
    for r in reports:
        assert r.status == "ok" and r.exit_code == 0, (r.day, r.errors)
        assert r.checks_passed and not r.late
    assert "agent 7 not built yet: skipped" in capsys.readouterr().out
    for d in DAYS:
        rows = conn.execute("SELECT agent_id, equity FROM equity WHERE as_of_date = ?", (d.isoformat(),)).fetchall()
        assert sorted(r[0] for r in rows) == sorted(EQUITY_IDS)
        sid = conn.execute("SELECT snapshot_id FROM snapshots WHERE snapshot_date = ?", (d.isoformat(),)).fetchone()[0]
        dec = conn.execute("SELECT agent_id, COUNT(*) FROM decisions WHERE snapshot_id = ? GROUP BY 1",
                           (sid,)).fetchall()
        assert dict(dec) == {a: 1 for a in EQUITY_IDS}
    # agents not built yet have nothing at all
    for aid in roster.EQUITY_NOT_BUILT:
        assert conn.execute("SELECT COUNT(*) FROM equity WHERE agent_id = ?", (aid,)).fetchone()[0] == 0
    # day 1: every agent started at $100k; agent 1's SPY order filled at day 2's open
    assert {r[0] for r in conn.execute("SELECT equity FROM equity WHERE as_of_date = ?",
                                       (START.isoformat(),))} == {L.START_CASH}
    f = conn.execute("""SELECT f.filled_at, f.reference_price, o.symbol, o.created_at FROM fills f
                        JOIN orders o USING (order_id) WHERE o.agent_id = '1'""").fetchall()
    assert len(f) == 1 and f[0]["symbol"] == "SPY"
    assert f[0]["filled_at"] == db.utc_ts(cal.session(DAYS[1], conn).open)
    assert f[0]["reference_price"] == pytest.approx(eq_close("SPY", DAYS[1]) * 0.995)
    # every order is filled by the next day's settle, except the last day's
    pending = conn.execute("""SELECT COUNT(*) FROM orders o WHERE NOT EXISTS
                              (SELECT 1 FROM fills f WHERE f.order_id = o.order_id)""").fetchone()[0]
    last = conn.execute("SELECT COUNT(*) FROM orders WHERE created_at >= ?",
                        (db.utc_ts(close_of(DAYS[-1])),)).fetchone()[0]
    assert pending == last
    # risk: agent 12's 30% AMZN clipped to 20% every day it rebalanced
    clip = conn.execute("SELECT * FROM risk_events WHERE agent_id = '12' AND rule = 'max_per_stock'").fetchall()
    assert clip and all(e["applied_weight"] == pytest.approx(0.2) for e in clip)
    # AI memory: day 2's prompt shows day 1's decision
    assert '"last_decisions":[{"action":"rebalance","date":"2026-11-16"' in clients["9"].calls[1]
    # input.run (v1.15): the label's end date, NYSE sessions left (Tue 17 .. Tue 24 Nov = 6 on day 1)
    for i, d in enumerate(DAYS):
        assert f'"run":{{"end_date":"2026-11-24","trading_days_left":{6 - i}}}' in clients["8"].calls[i], d
    # heartbeat: one run_id per run, every step started and finished
    for r in reports:
        rows = heartbeat(conn, r.run_id)
        jobs = {(j, e) for j, e, _ in rows}
        for step in ("daily_run", "settle", "decide"):
            assert (step, "started") in jobs and (step, "finished") in jobs, (r.day, step)
        for step in ("fetch_prices", "build_snapshot", "fetch_etb"):
            assert any(j == step for j, _ in jobs), (r.day, step)
        assert not any(e == "error" for _, e, _ in rows)
        start = rows[0]
        assert start[0] == "daily_run" and start[2]["skipped_not_built"] == ["7", "13", "14", "15"]
        for aid in EQUITY_IDS:
            assert (f"decide:{aid}", "finished") in jobs and (f"place_orders:{aid}", "finished") in jobs
    # decisions are timestamped at the decide time (close + 2:00), not before
    t = conn.execute("SELECT MIN(decided_at) FROM decisions d JOIN snapshots s USING (snapshot_id) "
                     "WHERE s.snapshot_date = ?", (START.isoformat(),)).fetchone()[0]
    assert t >= db.utc_ts(cal.equity_times(START, conn).decide_at)


# ---------------------------------------------------------------------------
# 2. Restart safety
# ---------------------------------------------------------------------------

def test_rerunning_a_day_is_a_no_op(conn, clients):
    for d in DAYS[:3]:
        equity_run(conn, d, clients)
    before, calls = counts(conn), n_calls(clients)
    for d in DAYS[:3]:                                     # again, each a little later
        r, _ = equity_run(conn, d, clients, at=cal.equity_times(d, conn).decide_at + timedelta(minutes=30))
        assert r.exit_code == 0, r.errors
        # placed agents are skipped; a hold with nothing to place is re-checked and places nothing again
        assert all(a.placed == "already_placed" or (a.placed == "placed" and not a.orders)
                   for a in r.agents.values())
    assert counts(conn) == before
    assert n_calls(clients) == calls                      # no AI call repeated


def test_rerun_after_a_failure_does_only_what_is_missing(conn, clients, monkeypatch):
    equity_run(conn, DAYS[0], clients)
    real = LD.place_orders

    def flaky(conn_, agent_id, *a, **kw):
        if agent_id in ("5", "9"):
            raise RuntimeError("disk full")
        return real(conn_, agent_id, *a, **kw)

    monkeypatch.setattr(LD, "place_orders", flaky)
    r, _ = equity_run(conn, DAYS[1], clients)
    assert r.exit_code == 1 and any("place_orders:5" in e for e in r.errors)
    calls = {a: len(c.calls) for a, c in clients.items()}
    decisions = counts(conn)["decisions"]
    monkeypatch.setattr(LD, "place_orders", real)
    r2, _ = equity_run(conn, DAYS[1], clients, at=cal.equity_times(DAYS[1], conn).decide_at + timedelta(minutes=5))
    assert r2.exit_code == 0, r2.errors
    assert r2.agents["5"].placed == "placed" and r2.agents["9"].placed == "placed"
    assert r2.agents["5"].decision.startswith("stored") and r2.agents["8"].placed == "already_placed"
    assert {a: len(c.calls) for a, c in clients.items()} == calls      # agent 9 not called again
    assert counts(conn)["decisions"] == decisions
    # agent 9's orders follow its stored, risk-checked targets
    o9 = conn.execute("SELECT symbol FROM orders WHERE agent_id = '9' AND created_at >= ?",
                      (db.utc_ts(close_of(DAYS[1])),)).fetchall()
    assert {r[0] for r in o9} <= set(AI_TARGETS["9"])


# ---------------------------------------------------------------------------
# 3. A snapshot that fails its checks: nobody decides, the ledger still settles
# ---------------------------------------------------------------------------

def test_failed_snapshot_check_no_decisions_but_ledger_settles(conn, clients):
    equity_run(conn, DAYS[0], clients)
    clock = Clock(cal.equity_times(DAYS[1], conn).bars_fetch_at)
    market = Market(clock, bad={("AAPL", DAYS[1]): True})       # high below the close: a bad bar
    calls = n_calls(clients)
    r, _ = equity_run(conn, DAYS[1], clients, market=market)
    assert r.checks_passed is False and r.exit_code == 1
    sid = r.snapshot_id
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE snapshot_id = ?", (sid,)).fetchone()[0] == 0
    assert n_calls(clients) == calls
    assert {x[0] for x in conn.execute("SELECT agent_id FROM equity WHERE as_of_date = ?",
                                       (DAYS[1].isoformat(),))} == set(EQUITY_IDS)
    assert conn.execute("SELECT COUNT(*) FROM fills WHERE filled_at = ?",
                        (db.utc_ts(cal.session(DAYS[1], conn).open),)).fetchone()[0] > 0   # day 1's orders filled
    rows = heartbeat(conn, r.run_id)
    assert any(j == "decide" and e == "error" and "failed its data checks" in d["reason"] for j, e, d in rows)
    assert all(a.placed in ("placed", "already_placed") for a in r.agents.values())   # margin check ran
    # once a corrected version of the bad bar is stored, the next day decides again
    fix = cal.equity_times(DAYS[1], conn).decide_at + timedelta(hours=3)
    insert_rows(conn, [bar_row("AAPL", DAYS[1], eq_close("AAPL", DAYS[1]), fix)])
    r3, _ = equity_run(conn, DAYS[2], clients)
    assert r3.exit_code == 0 and r3.decided


# ---------------------------------------------------------------------------
# 4. AI agents run in parallel; one timing out doesn't hold up the others
# ---------------------------------------------------------------------------

def test_one_ai_agent_times_out_while_the_others_finish(conn):
    barrier = threading.Barrier(len(roster.EQUITY_AI))
    clients = {a: Client(a, fail=(a == "10"), barrier=barrier) for a in roster.EQUITY_AI}
    r, _ = equity_run(conn, DAYS[0], clients)
    assert not barrier.broken                              # all five calls were in flight together
    assert r.agents["10"].decision == "unavailable" and r.agents["10"].attempts == 4
    assert len(clients["10"].calls) == 4                   # the call + 3 retries (§8)
    for aid in ("8", "9", "11", "12"):
        assert r.agents[aid].decision == "ok" and r.agents[aid].orders, aid
    rows = conn.execute("SELECT status, error FROM decisions WHERE agent_id = '10' ORDER BY attempt").fetchall()
    assert [x[0] for x in rows] == ["unavailable"] * 4 and "timeout" in rows[0][1]
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE agent_id = '10'").fetchone()[0] == 0
    assert r.exit_code == 1 and any(e.startswith("decide:10") for e in r.errors)
    assert all(r.agents[a].decision == "ok" for a in roster.EQUITY_RULE)


# ---------------------------------------------------------------------------
# 5. Late runs (protocol v1.13)
# ---------------------------------------------------------------------------

def test_a_run_three_hours_late_decides_and_is_labelled_late(conn, clients):
    times = cal.equity_times(DAYS[0], conn)
    r, clock = equity_run(conn, DAYS[0], clients, at=times.snapshot_at + timedelta(hours=3) - timedelta(minutes=1))
    assert r.exit_code == 0, r.errors
    assert r.late and r.minutes_late == 180 and r.decided
    snap = conn.execute("SELECT * FROM snapshots WHERE snapshot_id = ?", (r.snapshot_id,)).fetchone()
    p = json.loads(snap["payload"])
    assert p["late"] is True and p["minutes_late"] == 180
    assert snap["cutoff_ts"] == db.utc_ts(times.snapshot_at + timedelta(hours=3))
    assert p["symbols"]["AAPL"]["last_date"] == DAYS[0].isoformat()   # the late-fetched bar is in it
    assert counts(conn)["decisions"] == len(EQUITY_IDS)
    rows = heartbeat(conn, r.run_id)
    assert any(j == "late_run" and d["minutes_late"] == 180 for j, _, d in rows)
    # the orders fill as usual at the next open
    r2, _ = equity_run(conn, DAYS[1], clients)
    assert r2.exit_code == 0 and conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] > 0
    # a rerun of the late day rebuilds the same late snapshot: no-op
    before = counts(conn)
    r3, _ = equity_run(conn, DAYS[0], clients, at=times.snapshot_at + timedelta(hours=5))
    assert r3.exit_code == 0 and counts(conn) == before


def test_a_run_past_the_deadline_decides_nothing(conn, clients):
    deadline = cal.decision_deadline("us_equity", DAYS[0], conn)
    assert deadline == cal.session(DAYS[1], conn).open - timedelta(hours=1)
    r, _ = equity_run(conn, DAYS[0], clients, at=deadline + timedelta(minutes=1))
    assert not r.decided and r.exit_code == 1
    assert counts(conn)["decisions"] == 0 and counts(conn)["orders"] == 0 and n_calls(clients) == 0
    assert {x[0] for x in conn.execute("SELECT agent_id FROM equity")} == set(EQUITY_IDS)   # still marked
    assert any(j in ("build_snapshot", "decide") and e == "error" and "deadline" in d["reason"]
               for j, e, d in heartbeat(conn, r.run_id))


def test_agents_finishing_after_the_deadline_are_not_recorded(conn):
    times = cal.equity_times(DAYS[0], conn)
    deadline = cal.decision_deadline("us_equity", DAYS[0], conn)
    clock_box = {}

    class Slow(Client):
        def complete(self, prompt, *, model):
            clock_box["clock"].t = deadline + timedelta(minutes=1)      # the reply comes back too late
            return super().complete(prompt, model=model)

    clients = {a: (Slow(a) if a == "11" else Client(a)) for a in roster.EQUITY_AI}
    clock = Clock(deadline - timedelta(hours=2))
    clock_box["clock"] = clock
    market = Market(clock)
    deps = make_deps(clock, market, clients)
    # rule agents and the other AI agents are recorded before agent 11's slow reply moves the clock
    deps.client_for = lambda aid: clients[aid]
    r = daily.run_day(conn, "us_equity", DAYS[0], deps)
    assert r.late and r.agents["11"].decision == "past deadline"
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE agent_id = '11'").fetchone()[0] == 0
    assert times.snapshot_at < deadline


# ---------------------------------------------------------------------------
# 6. Crypto (§13): fill bars, settle, decide, orders
# ---------------------------------------------------------------------------

def crypto_run(conn, d, clients, at=None):
    clock = Clock(at or cal.crypto_times(d).bars_fetch_at)
    market = Market(clock)
    return daily.run_day(conn, "crypto", d, make_deps(clock, market, clients)), market


def test_crypto_end_to_end(conn, clients):
    days = [START + timedelta(days=i) for i in range(3)]
    reports = [crypto_run(conn, d, clients) for d in days]
    for r, m in reports:
        assert r.exit_code == 0, (r.day, r.errors)
    assert reports[0][1].fill_fetches == [] and reports[1][1].fill_fetches == [days[0]]
    btc = conn.execute("""SELECT f.*, o.created_at FROM fills f JOIN orders o USING (order_id)
                          WHERE o.agent_id = 'C0'""").fetchall()
    assert len(btc) == 1
    assert btc[0]["filled_at"] >= db.utc_ts(LD.ceil_minute(cal._parse_ts(btc[0]["created_at"])))
    assert btc[0]["fee_usd"] > 0
    c1 = conn.execute("SELECT * FROM risk_events WHERE agent_id = 'C1' AND rule = 'max_per_stock'").fetchall()
    assert c1 and c1[0]["applied_weight"] == pytest.approx(0.5)
    assert conn.execute("SELECT MIN(cash) FROM equity WHERE agent_id IN ('C0', 'C1')").fetchone()[0] >= 0
    before = counts(conn)
    for d in days:
        r, _ = crypto_run(conn, d, clients, at=cal.crypto_times(d).decide_at + timedelta(minutes=30))
        assert r.exit_code == 0
    assert counts(conn) == before


def crypto_fills(conn):
    return conn.execute("""SELECT o.agent_id, o.symbol, o.created_at, f.status, f.filled_at, f.reference_price,
                                  f.detail FROM orders o JOIN fills f USING (order_id)
                           ORDER BY o.agent_id, o.symbol""").fetchall()


def bar_open(sym, decision_date, minute=0):
    """The fake fill bar's open, `minute` minutes into its window (Market.fill_prices)."""
    return cr_close(sym, decision_date + timedelta(days=1)) * (1 + 0.0001 * minute)


def test_crypto_late_run_fills_at_the_first_bar_after_its_record_time(conn, clients):
    """Day 1 runs 3 hours late (decisions recorded ~03:10Z); day 2 on time fetches [t_ref, t_ref + 60)
    and both agents fill at the first bar at or after the recorded time, not at 00:15Z."""
    t = cal.crypto_times(START)
    r, _ = crypto_run(conn, START, clients, at=t.snapshot_at + timedelta(hours=3) - timedelta(minutes=1))
    assert r.late and r.minutes_late == 180 and r.decided and r.exit_code == 0
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 2
    rec = conn.execute("SELECT MAX(decided_at) FROM decisions").fetchone()[0]
    assert rec > db.utc_ts(t.decide_at + timedelta(hours=2, minutes=50))
    t_ref = LD.ceil_minute(cal._parse_ts(rec))
    r2, m2 = crypto_run(conn, START + timedelta(days=1), clients)
    assert r2.exit_code == 0 and m2.window_starts == [t_ref]
    got = [f for f in crypto_fills(conn) if f["created_at"] == rec]
    assert {(f["agent_id"], f["symbol"]) for f in got} == {("C0", "BTC/USD"), ("C1", "BTC/USD"), ("C1", "ETH/USD")}
    for f in got:
        assert f["status"] == "filled" and f["filled_at"] == db.utc_ts(t_ref)
        assert f["reference_price"] == pytest.approx(bar_open(f["symbol"], START))


def test_crypto_orders_share_one_t_ref_the_last_decision_record_time(conn, monkeypatch):
    """C0 decides at 00:15Z, C1's AI reply takes 7 minutes: both agents' orders carry C1's record time
    and fill at the same minute bar (one t_ref per day, the control included)."""
    box, c0_recorded = {}, threading.Event()
    record = daily._Day._record

    def spy(self, aid, *a, **kw):
        record(self, aid, *a, **kw)
        if aid == "C0":
            c0_recorded.set()

    monkeypatch.setattr(daily._Day, "_record", spy)

    class Slow(Client):
        def complete(self, prompt, *, model):
            assert c0_recorded.wait(10)                    # the reply comes after C0 is recorded
            box["clock"].sleep(7 * 60 + 13)
            return super().complete(prompt, model=model)

    clients = {"C1": Slow("C1")}
    t = cal.crypto_times(START)
    clock = Clock(t.bars_fetch_at)
    box["clock"] = clock
    r = daily.run_day(conn, "crypto", START, make_deps(clock, Market(clock), clients))
    assert r.exit_code == 0
    dec = {row["agent_id"]: row["decided_at"] for row in conn.execute("SELECT * FROM decisions")}
    assert dec["C0"] < dec["C1"] and cal._parse_ts(dec["C1"]) >= t.decide_at + timedelta(minutes=7)
    created = {row["agent_id"]: row["created_at"] for row in conn.execute("SELECT * FROM orders")}
    assert created == {"C0": dec["C1"], "C1": dec["C1"]}
    hb = [d for j, e, d in heartbeat(conn, r.run_id) if j == "crypto_t_ref"]
    assert hb == [{"t_ref": dec["C1"], "fill_from": db.utc_ts(LD.ceil_minute(cal._parse_ts(dec["C1"]))),
                   "agents": ["C0", "C1"]}]
    r2, m2 = crypto_run(conn, START + timedelta(days=1), clients)
    assert r2.exit_code == 0 and m2.window_starts == [LD.ceil_minute(cal._parse_ts(dec["C1"]))]
    assert len({f["filled_at"] for f in crypto_fills(conn)}) == 1


def test_crypto_coin_with_no_bar_in_the_window_is_cancelled_and_logged(conn, clients):
    """No ETH trade in the whole fetched window: C1's ETH order is cancelled, its BTC order fills."""
    crypto_run(conn, START, clients)
    clock = Clock(cal.crypto_times(START + timedelta(days=1)).bars_fetch_at)
    market = Market(clock)
    market.no_trades = {"ETH/USD"}
    r = daily.run_day(conn, "crypto", START + timedelta(days=1), make_deps(clock, market, clients))
    assert r.agents["C1"].settle == "settled"
    f = {(x["agent_id"], x["symbol"]): x for x in crypto_fills(conn)}
    assert f[("C1", "ETH/USD")]["status"] == "cancelled"
    assert json.loads(f[("C1", "ETH/USD")]["detail"])["reason"] == "no_fill_bar"
    assert f[("C1", "BTC/USD")]["status"] == "filled" and f[("C0", "BTC/USD")]["status"] == "filled"
    hb = [d for j, e, d in heartbeat(conn, r.run_id) if j == "settle:C1"]
    assert hb[0]["cancelled"] == ["ETH/USD"]


def test_crypto_past_the_deadline_decides_nothing_but_settles(conn, clients):
    crypto_run(conn, START, clients)
    d2 = START + timedelta(days=1)
    assert cal.decision_deadline("crypto", d2) == datetime(2026, 11, 18, 23, 15, tzinfo=UTC)
    r2, _ = crypto_run(conn, d2, clients, at=cal.decision_deadline("crypto", d2) + timedelta(minutes=1))
    assert not r2.decided and r2.exit_code == 1
    assert r2.agents["C0"].settle == "settled"           # day 1's orders still filled and marked
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 2   # day 1's only


# ---------------------------------------------------------------------------
# 7. Guards and defaults
# ---------------------------------------------------------------------------

def test_refuses_an_unlabelled_database_and_dates_before_the_start(tmp_path, conn, clients):
    bare = db.init_db(tmp_path / "bare.db")
    seed_calendar(bare)
    clock = Clock(cal.equity_times(START, bare).bars_fetch_at)
    with pytest.raises(ex.ExperimentError, match="no experiment label"):
        daily.run_day(bare, "us_equity", START, make_deps(clock, Market(clock), clients))
    bare.close()
    with pytest.raises(ex.ExperimentError, match="before the dev experiment's start"):
        equity_run(conn, date(2026, 11, 13), clients)
    assert counts(conn)["equity"] == 0


def test_official_never_runs_before_11_january(tmp_path, clients):
    c = db.init_db(tmp_path / "official.db")
    seed_calendar(c)
    with c:
        c.execute("INSERT INTO experiment (name, start_date) VALUES ('official', '2027-01-11')")
    clock = Clock(cal.equity_times(date(2027, 1, 8), c).bars_fetch_at)
    with pytest.raises(ex.ExperimentError, match="before the official experiment's start"):
        daily.run_day(c, "us_equity", date(2027, 1, 8), make_deps(clock, Market(clock), clients))
    c.close()


def test_market_holiday_runs_nothing(conn, clients):
    thanksgiving = date(2026, 11, 26)
    clock = Clock(datetime(2026, 11, 26, 23, 0, tzinfo=UTC))
    r = daily.run_day(conn, "us_equity", thanksgiving, make_deps(clock, Market(clock), clients))
    assert r.status == "market_closed" and r.exit_code == 0 and counts(conn)["equity"] == 0


def test_default_dates(conn):
    fri, sat = date(2026, 11, 20), datetime(2026, 11, 21, 12, tzinfo=UTC)
    assert daily.default_date("us_equity", sat, conn) == fri
    before_fetch = cal.equity_times(fri, conn).bars_fetch_at - timedelta(minutes=1)
    assert daily.default_date("us_equity", before_fetch, conn) == date(2026, 11, 19)
    assert daily.default_date("us_equity", before_fetch + timedelta(minutes=1), conn) == fri
    assert daily.default_date("crypto", datetime(2026, 11, 21, 0, 4, tzinfo=UTC), conn) == date(2026, 11, 19)
    assert daily.default_date("crypto", datetime(2026, 11, 21, 0, 5, tzinfo=UTC), conn) == date(2026, 11, 20)


def test_crypto_agent_deciding_after_the_days_orders_were_placed_is_missed(conn, clients, monkeypatch):
    """C1 fails to settle in the first run, so only C0 decides and places (t_ref fixed). A rerun an hour
    later settles and decides C1: it is missed for the day (no order, labelled late), no second t_ref."""
    real = LD.settle_day

    def flaky(conn_, agent_id, *a, **kw):
        if agent_id == "C1":
            raise LD.PriceNotAvailable("BTC/USD bar not stored yet")
        return real(conn_, agent_id, *a, **kw)

    monkeypatch.setattr(LD, "settle_day", flaky)
    r1, _ = crypto_run(conn, START, clients)
    assert r1.agents["C0"].placed == "placed" and r1.agents["C1"].settle == "error"
    t_ref = conn.execute("SELECT DISTINCT created_at FROM orders").fetchall()
    assert len(t_ref) == 1
    monkeypatch.setattr(LD, "settle_day", real)
    r2, _ = crypto_run(conn, START, clients, at=cal.crypto_times(START).decide_at + timedelta(hours=1))
    assert r2.agents["C1"].settle == "settled" and r2.agents["C1"].placed == "missed"
    assert r2.agents["C0"].placed == "already_placed"
    c1 = conn.execute("SELECT * FROM decisions WHERE agent_id = 'C1'").fetchall()
    assert len(c1) == 1 and c1[0]["decided_at"] > t_ref[0][0]               # decision logged, not traded
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE agent_id = 'C1'").fetchone()[0] == 0
    assert conn.execute("SELECT DISTINCT created_at FROM orders").fetchall() == t_ref
    hb = {j: (e, d) for j, e, d in heartbeat(conn, r2.run_id)}
    assert hb["crypto_t_ref"][1]["t_ref"] == t_ref[0][0] and hb["crypto_t_ref"][1]["missed"] == ["C1"]
    assert hb["place_orders:C1"][0] == "error" and hb["place_orders:C1"][1]["late"] is True
    r3, _ = crypto_run(conn, START, clients, at=cal.crypto_times(START).decide_at + timedelta(hours=2))
    assert r3.agents["C1"].placed == "missed" and counts(conn)["orders"] == 1   # a third run: still missed


def test_crypto_placement_failure_is_resumed_at_the_days_t_ref(conn, clients, monkeypatch):
    """C1 decided in the first run but its placement failed: the rerun places it at the same t_ref."""
    real = LD.place_orders

    def flaky(conn_, agent_id, *a, **kw):
        if agent_id == "C1":
            raise RuntimeError("disk full")
        return real(conn_, agent_id, *a, **kw)

    monkeypatch.setattr(LD, "place_orders", flaky)
    crypto_run(conn, START, clients)
    monkeypatch.setattr(LD, "place_orders", real)
    r2, _ = crypto_run(conn, START, clients, at=cal.crypto_times(START).decide_at + timedelta(hours=1))
    assert r2.agents["C1"].placed == "placed"
    assert len(conn.execute("SELECT DISTINCT created_at FROM orders").fetchall()) == 1


# ---------------------------------------------------------------------------
# §8 Claude reasoning gate (v1.14): the shakedown shadow call
# ---------------------------------------------------------------------------

def test_shadow_call_uses_agent_8s_exact_prompt_at_high_and_is_never_traded(conn, clients, monkeypatch):
    monkeypatch.setattr(roster, "SHADOW_EXPERIMENT", "dev")
    monkeypatch.setattr(roster, "SHADOW_START", DAYS[0])
    monkeypatch.setattr(roster, "SHADOW_END", DAYS[1])
    shadow = Client("8")
    clients = dict(clients, **{roster.SHADOW_KEY: shadow})
    seen = []
    real_agent = roster.shadow_agent
    monkeypatch.setattr(roster, "shadow_agent", lambda *a, **kw: seen.append(real_agent(*a, **kw)) or seen[-1])
    r, _ = equity_run(conn, DAYS[0], clients)
    assert r.exit_code == 0, r.errors
    assert seen[0].model.reasoning == "high" and seen[0].model.model_id == roster.model_for("8").model_id
    assert shadow.calls == clients["8"].calls                              # the identical prompt
    rows = conn.execute("SELECT * FROM shadow_decisions").fetchall()
    assert len(rows) == 1 and rows[0]["reasoning_setting"] == "high" and rows[0]["status"] == "ok"
    d8 = conn.execute("SELECT * FROM decisions WHERE agent_id = '8'").fetchone()
    assert json.loads(rows[0]["applied_targets"]) == daily.applied_weights(conn, d8)
    assert d8["reasoning_setting"] == "medium"
    n_orders = counts(conn)["orders"]
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE agent_id = '8'").fetchone()[0] == 3   # 8's only
    hb = [(e, d) for j, e, d in heartbeat(conn, r.run_id) if j == f"decide:{roster.SHADOW_KEY}"]
    assert [e for e, _ in hb] == ["started", "finished"] and hb[1][1]["cost_usd"] == pytest.approx(0.001)
    r2, _ = equity_run(conn, DAYS[0], clients, at=cal.equity_times(DAYS[0], conn).decide_at + timedelta(minutes=9))
    assert len(shadow.calls) == 1 and counts(conn)["orders"] == n_orders   # rerun: no second shadow call
    equity_run(conn, DAYS[1], clients)
    equity_run(conn, DAYS[2], clients)                                     # after SHADOW_END: none
    assert len(shadow.calls) == 2
    assert conn.execute("SELECT COUNT(*) FROM shadow_decisions").fetchone()[0] == 2


def test_no_shadow_call_outside_the_shakedown(conn, clients):
    r, _ = equity_run(conn, DAYS[0], clients)                              # experiment 'dev'
    assert r.exit_code == 0 and conn.execute("SELECT COUNT(*) FROM shadow_decisions").fetchone()[0] == 0
    assert not roster.shadow_active("official", "us_equity", date(2027, 1, 4))
    assert not roster.shadow_active("shakedown", "crypto", date(2026, 12, 20))
    assert roster.shadow_active("shakedown", "us_equity", date(2026, 12, 14))
    assert not roster.shadow_active("shakedown", "us_equity", date(2027, 1, 11))


def test_label_without_end_date_holds_ai_agents(tmp_path, clients):
    c = db.init_db(tmp_path / "old.db")
    seed_calendar(c)
    seed_history(c)
    with c:
        c.execute("INSERT INTO experiment (name, start_date) VALUES ('dev', ?)", (START.isoformat(),))
    r, _ = equity_run(c, START, clients)
    assert r.exit_code == 1 and any(e.startswith("run_info:") and "no end date" in e for e in r.errors)
    assert n_calls(clients) == 0                                   # no prompt is built without input.run
    for aid in roster.EQUITY_AI:
        assert r.agents[aid].decision == "invalid", aid
    for aid in roster.EQUITY_RULE:
        assert r.agents[aid].decision == "ok", aid
    c.close()
