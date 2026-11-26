"""Splits through the daily runner and the tick (§7 rule 11, v1.18), every agent, no network.

NVDA (held by AI agents 8 and 15 from day 1) splits 4:1 on Wed 18 Nov 2026. The fake market
announces it on the ex-date and serves NVDA's raw bars / 4 from then on. Alpaca's re-adjusted
history (adj = raw / 4 before the ex-date) arrives with the ex-date's ingest (on time) or the next
day's (the lag case), as new price versions, the way the ingest's history re-fetch stores them.
"""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dashboard import status_page as sp  # noqa: E402
from data import corporate_actions as ca  # noqa: E402
from data import db  # noqa: E402
from runner import alerts, status  # noqa: E402

import test_runner as TR  # noqa: E402
import test_tick as TT  # noqa: E402
from test_snapshot import close_of  # noqa: E402

UTC = timezone.utc
SYM, EX, RATIO = "NVDA", date(2026, 11, 18), 4.0
DAYS = [date(2026, 11, d) for d in (16, 17, 18, 19, 20, 23)]


class SplitMarket(TR.Market):
    adjust_on = EX                       # the ingest day on which Alpaca's history is re-adjusted

    def ingest(self, conn, asset_class, run_id, now):
        if asset_class != "us_equity":
            return super().ingest(conn, asset_class, run_id, now)
        t = now()
        d = max(x for x in TR.EQ_HIST if close_of(x) <= t)
        if d >= EX:
            self.missing.add((SYM, d))                         # stored below, post-split
        code = super().ingest(conn, asset_class, run_id, lambda: t)
        rows = []
        if d >= EX and not conn.execute("SELECT 1 FROM prices WHERE symbol = ? AND bar_date = ?",
                                        (SYM, d.isoformat())).fetchone():
            rows.append(TR.bar_row(SYM, d, TR.eq_close(SYM, d) / RATIO, t))
            rows[-1]["volume"] = rows[-1]["adj_volume"] = 1e6 * RATIO
        if d >= self.adjust_on:
            last = ca.bar_before(conn, SYM, EX.isoformat(), t)
            if last is not None and last["adj_close"] == last["close"]:      # not re-adjusted yet
                for x in TR.EQ_HIST:
                    if x < EX and x <= d:
                        r = TR.bar_row(SYM, x, TR.eq_close(SYM, x), t)
                        r.update({f"adj_{k}": r[k] / RATIO for k in ("open", "high", "low", "close")})
                        r["adj_volume"] = r["volume"] * RATIO
                        rows.append(r)
        TR.insert_rows(conn, rows)
        if d >= EX:
            with conn:
                conn.execute("""INSERT OR IGNORE INTO corporate_actions (ca_key, ca_type, symbol, ex_date, old_rate,
                                new_rate, raw, fetched_at) VALUES (?, 'forward_split', ?, ?, 1, ?, '{}', ?)""",
                             (f"forward_split|{SYM}|{EX}", SYM, EX.isoformat(), RATIO, db.utc_ts(t)))
        return code


class LagMarket(SplitMarket):
    adjust_on = EX + timedelta(days=1)


def run_world(tmp_path, market):
    path = TT.make_exp(tmp_path)
    TT.activate(tmp_path, "dev")
    w = TT.World(tmp_path, market=market)
    c = db.connect(path)
    for d in DAYS:
        fetch = TT.eq_fetch(d, c)
        w.tick(fetch)
        w.tick(fetch + timedelta(minutes=40))
    c.close()
    return path


def equity(path):
    c = db.connect(path)
    try:
        return {(r[0], r[1]): r[2] for r in c.execute(
            "SELECT agent_id, as_of_date, equity FROM equity e WHERE equity_id = (SELECT MAX(equity_id) FROM equity q "
            "WHERE q.agent_id = e.agent_id AND q.as_of_date = e.as_of_date)")}
    finally:
        c.close()


def every_agent_marked_every_day(path):
    eq = equity(path)
    missing = [(a, d.isoformat()) for d in DAYS for a in TR.EQUITY_IDS if (a, d.isoformat()) not in eq]
    return missing


def outcomes(path, now):
    c = db.connect(path)
    try:
        st = status.summary(c, now, trading=TR.no_network)
        return {(x["asset_class"], x["date"]): x["outcome"] for x in st.days}
    finally:
        c.close()


@pytest.fixture(scope="module")
def worlds(tmp_path_factory):
    return {name: run_world(tmp_path_factory.mktemp(name), m)
            for name, m in (("plain", TR.Market), ("split", SplitMarket), ("lag", LagMarket))}


def holders(path, d):
    c = db.connect(path)
    try:
        return {r[0]: r[1] for r in c.execute("SELECT agent_id, qty FROM positions WHERE symbol = ? AND as_of_date = ?",
                                              (SYM, d.isoformat()))}
    finally:
        c.close()


def test_on_time_split_leaves_every_equity_path_as_without_it(worlds):
    plain, split = equity(worlds["plain"]), equity(worlds["split"])
    assert plain.keys() == split.keys()
    for k in plain:
        assert split[k] == pytest.approx(plain[k], rel=1e-9, abs=1e-4), k     # only market moves and costs
    before = holders(worlds["plain"], EX - timedelta(days=1))
    assert before                                                           # someone holds NVDA across it
    c = db.connect(worlds["split"])
    rows = {r["agent_id"]: r for r in c.execute("SELECT * FROM split_events")}
    assert set(rows) == set(TR.EQUITY_IDS)                                  # every equity agent passed it once
    for a, q in before.items():
        assert (rows[a]["applied_on"], rows[a]["qty_before"]) == (EX.isoformat(), q)
        assert rows[a]["qty_after"] == pytest.approx(q * RATIO)
    assert ca.open_freezes(c, datetime(2026, 11, 24, tzinfo=UTC)) == []
    c.close()


def test_lag_case_freezes_for_a_day_then_applies_the_split(worlds):
    path = worlds["lag"]
    assert every_agent_marked_every_day(path) == []
    c = db.connect(path)
    ev = c.execute("SELECT * FROM symbol_events ORDER BY symbol_event_id").fetchall()
    assert [(e["event"], e["ex_date"]) for e in ev] == [("freeze", EX.isoformat()), ("split", EX.isoformat())]
    assert "not confirmed" in ev[0]["reason"] and ev[1]["corrects_id"] == ev[0]["symbol_event_id"]
    tue, wed, thu = EX - timedelta(days=1), EX, EX + timedelta(days=1)
    held = holders(path, tue)
    assert held
    for a, q in held.items():
        p = c.execute("SELECT * FROM positions WHERE agent_id = ? AND symbol = ? AND as_of_date = ?",
                      (a, SYM, wed.isoformat())).fetchone()
        # frozen on the ex-date: held, at its last close before the event
        assert p["qty"] == q and p["mark_price"] == pytest.approx(TR.eq_close(SYM, tue))
        s = c.execute("SELECT * FROM split_events WHERE agent_id = ?", (a,)).fetchone()
        assert (s["ex_date"], s["applied_on"], s["qty_before"]) == (wed.isoformat(), thu.isoformat(), q)
        p = c.execute("SELECT * FROM positions WHERE agent_id = ? AND symbol = ? AND as_of_date = ?",
                      (a, SYM, thu.isoformat())).fetchone()
        assert p["qty"] == pytest.approx(q * RATIO) and p["mark_price"] == pytest.approx(TR.eq_close(SYM, thu) / RATIO)
    # equity continuous: on the ex-date the NVDA leg is flat (held at Tuesday's close) ...
    plain, lag = equity(worlds["plain"]), equity(path)
    for a in TR.EQUITY_IDS:
        assert lag[(a, tue.isoformat())] == pytest.approx(plain[(a, tue.isoformat())], rel=1e-9)
    for a, q in held.items():
        frozen_leg = q * TR.eq_close(SYM, tue)
        true_leg = q * TR.eq_close(SYM, wed)
        assert lag[(a, wed.isoformat())] - plain[(a, wed.isoformat())] == pytest.approx(frozen_leg - true_leg, abs=0.05)
    # ... and every pending NVDA order across the ex-date was cancelled as frozen, new ones rejected
    rejected = c.execute("SELECT COUNT(*) FROM risk_events WHERE rule = 'symbol_frozen'").fetchone()[0]
    cancelled = c.execute("SELECT COUNT(*) FROM fills WHERE json_extract(detail, '$.reason') = 'symbol_frozen'"
                          ).fetchone()[0]
    assert rejected + cancelled > 0
    c.close()


def test_a_freeze_never_makes_later_days_wait(worlds):
    for name in ("split", "lag"):
        path = worlds[name]
        assert every_agent_marked_every_day(path) == [], name
        o = outcomes(path, datetime(2026, 11, 24, 12, tzinfo=UTC))
        bad = {k: v for k, v in o.items() if k[0] == "us_equity" and v not in ("ok", "pending")}
        assert bad == {}, name


def test_freeze_shows_on_the_status_page_summary_and_alerts(tmp_path):
    path = TT.make_exp(tmp_path)
    TT.activate(tmp_path, "dev --no-ai")
    w = TT.World(tmp_path, market=LagMarket)
    c = db.connect(path)
    for d in DAYS[:3]:                                        # through the ex-date: frozen
        fetch = TT.eq_fetch(d, c)
        w.tick(fetch)
    now = TT.eq_fetch(EX, c) + timedelta(hours=1)
    alerts.check_freezes(c, now)
    key = f"freeze:{SYM}:{EX}"
    a = c.execute("SELECT * FROM alerts WHERE key = ?", (key,)).fetchone()
    assert (a["event"], a["priority"]) == ("open", alerts.HIGH) and "not confirmed" in a["body"]
    assert alerts.freeze_lines(c, now) and SYM in alerts.freeze_lines(c, now)[0]
    d = sp.load(tmp_path, now, tmp_path / "none.json", trading=TR.no_network)
    assert [f["symbol"] for f in d["freezes"]] == [SYM] and d["freezes"][0]["held_at"] == pytest.approx(
        TR.eq_close(SYM, EX - timedelta(days=1)))
    assert "Frozen stocks" in sp.render(d) and "not confirmed" in sp.render(d)
    # the next day confirms it: unfrozen, alert resolved once
    fetch = TT.eq_fetch(DAYS[3], c)
    w.tick(fetch)
    alerts.check_freezes(c, fetch + timedelta(hours=1))
    alerts.check_freezes(c, fetch + timedelta(hours=2))
    assert [r[0] for r in c.execute("SELECT event FROM alerts WHERE key = ? ORDER BY alert_id", (key,))] == \
        ["open", "resolved"]
    assert sp.load(tmp_path, fetch + timedelta(hours=1), tmp_path / "none.json", trading=TR.no_network)["freezes"] == []
    c.close()


def _position_line(prompt, sym):
    start = prompt.index("{", prompt.rindex("\n{") if "\n{" in prompt else 0)
    data = json.loads(prompt[start:])
    return data["portfolio"]["positions"].get(sym)


def test_ai_agents_are_prompted_with_the_frozen_position(tmp_path):
    path = TT.make_exp(tmp_path)
    TT.activate(tmp_path, "dev")
    w = TT.World(tmp_path, market=LagMarket)
    c = db.connect(path)
    seen = {}
    for d in DAYS[:4]:                                          # Mon..Thu: frozen on Wed, split applied Thu
        fetch = TT.eq_fetch(d, c)
        n = {a: len(w.clients[a].calls) for a in ("8", "15")}
        w.tick(fetch)
        for a in ("8", "15"):
            new = w.clients[a].calls[n[a]:]
            if new:
                seen[(a, d)] = _position_line(new[-1], SYM)
    tue, wed, thu = DAYS[1], DAYS[2], DAYS[3]
    for a in ("8", "15"):
        assert seen[(a, tue)] is not None and "frozen" not in seen[(a, tue)]
        p = seen[(a, wed)]
        assert p["frozen"] is True and p["last_close"] == pytest.approx(TR.eq_close(SYM, tue), rel=1e-6)
        assert p["unrealised_pnl"] == pytest.approx(p["shares"] * (p["last_close"] - p["avg_cost"]), rel=1e-6, abs=0.01)
        assert "frozen" not in seen[(a, thu)]                    # lifted: post-split shares at the close
        assert seen[(a, thu)]["shares"] == pytest.approx(4 * p["shares"], rel=1e-6)
    c.close()
