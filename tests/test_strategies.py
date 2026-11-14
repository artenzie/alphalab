"""Agents 1 and 2 (protocol §5) and the shared interface (strategies/base.py).

No network, no database: snapshots are built directly from the Snapshot
dataclass with synthetic bars. The 2027 run calendar is written out by hand
(holidays MLK Day 18 Jan, Presidents' Day 15 Feb, Good Friday 26 Mar) and,
when the real database is present, checked against its cached calendar.
"""

import ast
import builtins
import math
import os
import socket
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data.snapshot import Bar, Snapshot, canonical_json, compute_fields, sha256  # noqa: E402
from strategies import benchmark, equal_weight, registry  # noqa: E402
from strategies.base import (Context, Decision, DecisionError, Portfolio,  # noqa: E402
                             build_context)

STOCKS = ["AAPL", "AMZN", "BRK.B", "CAT", "COST", "CVX", "GE", "GOOGL", "JNJ", "JPM", "LIN",
          "LLY", "META", "NEE", "NEM", "NVDA", "PLD", "SO", "TSLA", "WELL", "WMT", "XOM"]
UNIVERSE = STOCKS + ["SPY"]
HOLIDAYS_2027 = {date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26)}
RUN_START, RUN_END = date(2027, 1, 11), date(2027, 4, 2)
RUN = [RUN_START + timedelta(days=i) for i in range((RUN_END - RUN_START).days + 1)]
RUN = [d for d in RUN if d.weekday() < 5 and d not in HOLIDAYS_2027]
REAL_DB = ROOT / "data" / "alphalab.db"

EMPTY = Portfolio(cash=100_000.0, value=100_000.0)
IN_SPY = Portfolio(cash=0.0, positions={"SPY": 130.0}, value=100_000.0)
IN_STOCKS = Portfolio(cash=0.0, positions={s: 10.0 for s in STOCKS}, value=100_000.0)


def make_snapshot(d: date, symbols=UNIVERSE, n=120) -> Snapshot:
    bars = {s: tuple(Bar((d - timedelta(days=n - i)).isoformat(), 100 + i, 101 + i, 99 + i, 100 + i, 1e6)
                     for i in range(n)) for s in sorted(symbols)}
    hist = sha256(canonical_json({s: [list(b) for b in bars[s]] for s in bars}))
    payload = {"version": 1, "asset_class": "us_equity", "decision_date": d.isoformat(), "mode": "backtest",
               "cutoff_ts": f"{d.isoformat()}T22:45:00.000Z", "history_sha256": hist,
               "symbols": {s: compute_fields(list(bars[s]), "us_equity") for s in bars}}
    text = canonical_json(payload)
    return Snapshot("us_equity", d.isoformat(), "backtest", payload["cutoff_ts"], text, sha256(text), hist, bars)


def ctx(d: date) -> Context:
    return build_context(d, [s for s in RUN if s <= d])


def run_agent(agent, holdings_after_first_buy: Portfolio):
    """Decisions on every run session; the portfolio is invested from the day after the first buy."""
    out, portfolio = {}, EMPTY
    for d in RUN:
        dec = agent(make_snapshot(d), portfolio, ctx(d))
        out[d] = dec
        if not dec.is_hold:
            portfolio = holdings_after_first_buy
    return out


def total(weights) -> float:
    return math.fsum(weights.values())


# ---------------------------------------------------------------------------
# The calendar and build_context
# ---------------------------------------------------------------------------

def test_run_calendar_has_57_sessions():
    assert len(RUN) == 57 and RUN[0] == RUN_START and RUN[-1] == RUN_END


@pytest.mark.skipif(not REAL_DB.exists(), reason="no real database")
def test_run_calendar_matches_cached_calendar():
    conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT DISTINCT session_date FROM market_sessions "
                            "WHERE session_date BETWEEN ? AND ? ORDER BY session_date",
                            (RUN_START.isoformat(), RUN_END.isoformat())).fetchall()
    finally:
        conn.close()
    if not rows:
        pytest.skip("calendar for 2027 not cached")
    assert [date.fromisoformat(r[0]) for r in rows] == RUN


def test_first_day_of_run_sets_every_flag():
    c = ctx(RUN_START)
    assert c.is_first_day_of_run and c.is_first_session_of_week and c.is_first_session_of_month


def test_week_and_month_flags():
    weeks = [d for d in RUN if ctx(d).is_first_session_of_week]
    months = [d for d in RUN if ctx(d).is_first_session_of_month]
    assert date(2027, 1, 19) in weeks       # Tuesday, after MLK Day
    assert date(2027, 2, 16) in weeks       # Tuesday, after Presidents' Day
    assert date(2027, 1, 18) not in RUN and date(2027, 1, 20) not in weeks
    assert len(weeks) == 12                 # 12-week run
    assert months == [RUN_START, date(2027, 2, 1), date(2027, 3, 1), date(2027, 4, 1)]
    assert all(not ctx(d).is_first_day_of_run for d in RUN[1:])


def test_month_starting_after_weekend():
    # Sat 1 May 2027 -> first session Mon 3 May; Fri 30 Apr is not first of month
    sessions = [date(2027, 4, 29), date(2027, 4, 30), date(2027, 5, 3)]
    assert build_context(date(2027, 5, 3), sessions).is_first_session_of_month
    assert not build_context(date(2027, 4, 30), sessions[:2]).is_first_session_of_month


def test_week_flag_uses_iso_week_across_year_end():
    # Thu 31 Dec 2026 and Mon 4 Jan 2027 are in different ISO weeks
    c = build_context(date(2027, 1, 4), [date(2026, 12, 31), date(2027, 1, 4)])
    assert c.is_first_session_of_week and c.is_first_session_of_month and not c.is_first_day_of_run


def test_build_context_rejects_bad_sessions():
    with pytest.raises(ValueError):
        build_context(date(2027, 1, 12), [date(2027, 1, 11)])
    with pytest.raises(ValueError):
        build_context(date(2027, 1, 12), [date(2027, 1, 12), date(2027, 1, 12)])
    with pytest.raises(ValueError):
        build_context(date(2027, 1, 12), [])


# ---------------------------------------------------------------------------
# Decision and Portfolio
# ---------------------------------------------------------------------------

def test_decision_validation():
    with pytest.raises(DecisionError):
        Decision({"SPY": 1.0}, "")
    with pytest.raises(DecisionError):
        Decision({"SPY": 1.0}, "two\nlines")
    for bad in (float("nan"), float("inf"), "1.0", True, None):
        with pytest.raises(DecisionError):
            Decision({"SPY": bad}, "x")
    assert Decision.hold("waiting").is_hold


def test_decision_and_inputs_are_read_only():
    dec = Decision({"SPY": 1.0}, "x")
    with pytest.raises(TypeError):
        dec.weights["SPY"] = 2.0
    with pytest.raises(AttributeError):
        dec.reason = "y"
    with pytest.raises(TypeError):
        IN_SPY.positions["SPY"] = 0
    with pytest.raises(AttributeError):
        ctx(RUN_START).is_first_day_of_run = False


# ---------------------------------------------------------------------------
# Agent 1 · Benchmark
# ---------------------------------------------------------------------------

def test_benchmark_buys_spy_on_day_1_only_then_holds():
    decs = run_agent(benchmark.decide, IN_SPY)
    buys = [d for d, dec in decs.items() if not dec.is_hold]
    assert buys == [RUN_START]
    w = decs[RUN_START].weights
    assert dict(w) == {"SPY": 1.0} and total(w) == 1.0
    assert all(decs[d].weights is None for d in RUN[1:])


def test_benchmark_day_1_buys_even_if_somehow_holding_spy():
    assert benchmark.decide(make_snapshot(RUN_START), IN_SPY, ctx(RUN_START)).weights == {"SPY": 1.0}


def test_benchmark_catches_missed_day_1():
    day2 = RUN[1]
    dec = benchmark.decide(make_snapshot(day2), EMPTY, ctx(day2))
    assert dict(dec.weights) == {"SPY": 1.0} and "missed" in dec.reason
    # holding other things but no SPY still counts as not invested in the benchmark
    other = Portfolio(cash=0.0, positions={"AAPL": 5.0}, value=100_000.0)
    assert benchmark.decide(make_snapshot(day2), other, ctx(day2)).weights == {"SPY": 1.0}
    # and once SPY is held it holds
    assert benchmark.decide(make_snapshot(RUN[2]), IN_SPY, ctx(RUN[2])).is_hold


def test_benchmark_missed_first_two_days_buys_on_third():
    decs, portfolio = {}, EMPTY
    for i, d in enumerate(RUN[:6]):
        dec = benchmark.decide(make_snapshot(d), portfolio, ctx(d))
        decs[d] = dec
        if i >= 2 and not dec.is_hold:      # days 1 and 2 never filled
            portfolio = IN_SPY
    assert [d for d, dec in decs.items() if not dec.is_hold] == RUN[:3]


# ---------------------------------------------------------------------------
# Agent 2 · Equal-weight
# ---------------------------------------------------------------------------

def test_equal_weight_triggers_day_1_and_first_session_of_each_month():
    decs = run_agent(equal_weight.decide, IN_STOCKS)
    buys = [d for d, dec in decs.items() if not dec.is_hold]
    assert buys == [RUN_START, date(2027, 2, 1), date(2027, 3, 1), date(2027, 4, 1)]
    for d in buys:
        w = decs[d].weights
        assert sorted(w) == STOCKS and "SPY" not in w and len(w) == 22
        assert all(x == 1 / 22 for x in w.values())
        assert math.isclose(total(w), 1.0, rel_tol=0, abs_tol=1e-12)
    assert all(decs[d].is_hold for d in RUN if d not in buys)


def test_equal_weight_catches_missed_first_decision():
    day2 = RUN[1]
    dec = equal_weight.decide(make_snapshot(day2), EMPTY, ctx(day2))
    assert len(dec.weights) == 22 and "missed" in dec.reason
    # SPY alone doesn't count as invested in the stocks
    assert not equal_weight.decide(make_snapshot(day2), IN_SPY, ctx(day2)).is_hold
    assert equal_weight.decide(make_snapshot(day2), IN_STOCKS, ctx(day2)).is_hold


def test_equal_weight_uses_only_snapshot_symbols():
    d = date(2027, 2, 1)
    dec = equal_weight.decide(make_snapshot(d, ["AAPL", "NVDA", "SPY"]), IN_STOCKS, ctx(d))
    assert dict(dec.weights) == {"AAPL": 0.5, "NVDA": 0.5}


# ---------------------------------------------------------------------------
# Isolation: an agent sees only the snapshot, portfolio and context it's given
# ---------------------------------------------------------------------------

AGENT_MODULES = sorted(p for p in (ROOT / "strategies").glob("*.py")
                       if p.stem not in {"__init__", "base", "registry"})
ALLOWED_IMPORTS = {"__future__", "strategies.base"}


@pytest.mark.parametrize("path", AGENT_MODULES, ids=lambda p: p.stem)
def test_agent_modules_import_only_the_interface(path):
    tree = ast.parse(path.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported <= ALLOWED_IMPORTS, imported - ALLOWED_IMPORTS
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"open", "eval", "exec", "__import__", "globals", "getattr", "compile"}


def sealed(monkeypatch):
    """No files, database, network, environment or new imports while an agent decides."""
    def boom(*a, **k):
        raise AssertionError("agent tried to reach outside its inputs")
    real_import = builtins.__import__
    loaded = set(sys.modules)

    def guarded_import(name, *a, **k):
        if name not in loaded:
            raise AssertionError(f"agent imported {name}")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "open", boom)
    monkeypatch.setattr(sqlite3, "connect", boom)
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(os, "getenv", boom)
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(builtins, "__import__", guarded_import)


@pytest.mark.parametrize("agent_id", sorted(registry.AGENTS))
def test_agents_run_sealed(agent_id, monkeypatch):
    """Same decisions with every outside channel blocked as with none blocked."""
    agent = registry.get_agent(agent_id)
    cases = [(make_snapshot(d), p, ctx(d)) for d in RUN for p in (EMPTY, IN_SPY, IN_STOCKS)]
    expected = [agent(*c) for c in cases]
    sealed(monkeypatch)
    got = [agent(*c) for c in cases]
    monkeypatch.undo()
    assert got == expected and any(not g.is_hold for g in got)


@pytest.mark.parametrize("agent_id", sorted(registry.AGENTS))
def test_agents_are_pure(agent_id):
    agent = registry.get_agent(agent_id)
    for d in (RUN_START, RUN[1], date(2027, 2, 1), RUN[-1]):
        snap, c = make_snapshot(d), ctx(d)
        before = (snap.canonical, snap.sha256, dict(snap._history))
        a, b = agent(snap, EMPTY, c), agent(snap, EMPTY, c)
        assert a == b
        assert (snap.canonical, snap.sha256, dict(snap._history)) == before
        # an identical snapshot built separately gives the same decision
        assert agent(make_snapshot(d), EMPTY, ctx(d)) == a


def test_snapshot_has_no_connection():
    snap = make_snapshot(RUN_START)
    assert not any(isinstance(v, sqlite3.Connection) for v in vars(snap).values())


def test_registry():
    assert sorted(registry.AGENTS) == [1, 2, 3, 4, 5, 6]
    assert registry.AGENTS[1] is benchmark.decide and registry.AGENTS[2] is equal_weight.decide
    assert len(AGENT_MODULES) == len(registry.AGENTS)
    with pytest.raises(KeyError):
        registry.get_agent(99)
    with pytest.raises(TypeError):
        registry.AGENTS[3] = benchmark.decide
