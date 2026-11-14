"""Risk checks (protocol §6 v1.4, §13): execution/risk.py on hand-made decisions.

Every rule on its own, the order of checks, exact pro rata maths, hold
pass-through, the easy-to-borrow rule against current weights, margin, the
"never larger than asked" invariant, purity, and that every event fits the
risk_events table. The real-data clip count is in scripts/backtest_risk.py.
"""

import ast
import json
import random
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from execution import risk  # noqa: E402
from execution.risk import apply_risk, margin_check  # noqa: E402
from strategies.base import Decision  # noqa: E402
from test_strategies import sealed  # noqa: E402

def etb(*symbols, flag=True):
    return {s: flag for s in symbols}


def run(agent_id, weights, flags=None, current=None):
    flags = etb(*weights) if flags is None else flags
    return apply_risk(agent_id, Decision(weights, "test"), flags, current or {})


def rules(events):
    return [(e["event_type"], e["rule"], e["symbol"]) for e in events]


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

def test_every_agent_has_a_profile_matching_section_6():
    expected = {
        "1": (1.0, False, 1.0, 0.0), "2": (1 / 22, False, 1.0, 0.0),
        **{a: (0.20, False, 1.0, 0.0) for a in ("3", "5", "7")},
        **{a: (0.20, True, 1.0, 0.30) for a in ("4", "8", "9", "10", "11", "12")},
        **{a: (0.20, True, 1.0, 0.50) for a in ("6", "13")},
        "14": (0.10, False, 1.0, 0.0), "15": (0.30, True, 1.5, 0.50),
        "C0": (1.0, False, 1.0, 0.0), "C1": (0.50, False, 1.0, 0.0),
    }
    got = {a: (p.max_per_stock, p.shorts, p.max_gross, p.max_total_short) for a, p in risk.PROFILES.items()}
    assert got == expected


def test_profiles_match_the_schema_risk_profile_column():
    conn = sqlite3.connect(":memory:")
    conn.executescript((ROOT / "data" / "schema.sql").read_text())
    db = dict(conn.execute("SELECT agent_id, risk_profile FROM agents"))
    assert db == {a: p.name for a, p in risk.PROFILES.items()}


def test_int_and_str_agent_ids_and_unknown_agent():
    assert risk.profile(15) is risk.profile("15")
    with pytest.raises(KeyError):
        run(16, {"A": 0.1})


# ---------------------------------------------------------------------------
# Hold and no-op
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("agent_id", sorted(risk.PROFILES))
def test_hold_passes_through_untouched(agent_id):
    assert apply_risk(agent_id, Decision.hold("hold"), {}, {"A": 0.9, "B": -0.9}) == (None, [])


def test_within_limits_unchanged_and_no_events():
    w = {"A": 0.2, "B": 0.2, "C": -0.1, "D": -0.2, "E": 0.0}
    out, ev = run(4, w)
    assert out == w and ev == []
    assert out is not w


# ---------------------------------------------------------------------------
# 1. Shorts not allowed
# ---------------------------------------------------------------------------

def test_agent_14_shorts_rejected():
    out, ev = run(14, {"A": 0.10, "B": -0.05, "C": -0.10})
    assert out == {"A": 0.10, "B": 0.0, "C": 0.0}
    assert rules(ev) == [("reject", "shorts_not_allowed", "B"), ("reject", "shorts_not_allowed", "C")]
    assert ev[0]["requested_weight"] == -0.05 and ev[0]["applied_weight"] == 0.0


@pytest.mark.parametrize("agent_id", ["1", "2", "3", "5", "7", "14", "C0", "C1"])
def test_long_only_agents_reject_every_short(agent_id):
    out, ev = run(agent_id, {"X": -0.01})
    assert out == {"X": 0.0} and rules(ev) == [("reject", "shorts_not_allowed", "X")]


def test_shorts_not_allowed_even_if_already_held():
    out, _ = run(3, {"X": -0.05}, current={"X": -0.05})
    assert out == {"X": 0.0}


# ---------------------------------------------------------------------------
# 2. Easy-to-borrow
# ---------------------------------------------------------------------------

def test_new_short_in_hard_to_borrow_stock_rejected():
    out, ev = run(4, {"A": 0.1, "B": -0.1}, flags={"A": True, "B": False})
    assert out == {"A": 0.1, "B": 0.0}
    assert rules(ev) == [("reject", "easy_to_borrow", "B")]
    assert json.loads(ev[0]["detail"])["in_flags"] is True


def test_symbol_missing_from_flags_counts_as_not_easy_to_borrow():
    out, ev = run(4, {"B": -0.1}, flags={})
    assert out == {"B": 0.0}
    assert json.loads(ev[0]["detail"])["in_flags"] is False


def test_easy_to_borrow_short_allowed():
    out, ev = run(4, {"B": -0.1}, flags={"B": True})
    assert out == {"B": -0.1} and ev == []


def test_existing_short_kept_when_stock_stops_being_easy_to_borrow():
    """§6: risk checks never add trades the agent didn't ask for, so holding a short is allowed."""
    out, ev = run(4, {"B": -0.08}, flags={"B": False}, current={"B": -0.08})
    assert out == {"B": -0.08} and ev == []
    out, ev = run(4, {"B": -0.05}, flags={"B": False}, current={"B": -0.08})   # a reduction is fine
    assert out == {"B": -0.05} and ev == []


def test_increasing_a_hard_to_borrow_short_kept_at_current_size():
    out, ev = run(4, {"B": -0.10}, flags={"B": False}, current={"B": -0.08})
    assert out == {"B": -0.08}
    assert rules(ev) == [("reject", "easy_to_borrow", "B")]
    assert ev[0]["requested_weight"] == -0.10 and ev[0]["applied_weight"] == -0.08


def test_flip_from_long_to_short_in_hard_to_borrow_stock_rejected():
    out, ev = run(4, {"B": -0.1}, flags={"B": False}, current={"B": 0.1})
    assert out == {"B": 0.0} and rules(ev) == [("reject", "easy_to_borrow", "B")]


def test_longs_never_need_easy_to_borrow():
    out, ev = run(3, {"A": 0.2}, flags={"A": False})
    assert out == {"A": 0.2} and ev == []


# ---------------------------------------------------------------------------
# 3. Per-stock cap
# ---------------------------------------------------------------------------

def test_per_stock_cap_cuts_only_that_position():
    out, ev = run(3, {"A": 0.25, "B": 0.20, "C": 0.10})
    assert out == {"A": 0.20, "B": 0.20, "C": 0.10}
    assert rules(ev) == [("clip", "max_per_stock", "A")]
    assert ev[0]["requested_weight"] == 0.25 and ev[0]["applied_weight"] == 0.20


def test_per_stock_cap_applies_to_shorts_keeping_the_sign():
    out, _ = run(15, {"A": -0.40})
    assert out == {"A": -0.30}


def test_c1_short_rejected_and_60pct_coin_cut_to_50():
    out, ev = run("C1", {"BTC/USD": 0.60, "ETH/USD": -0.20, "SOL/USD": 0.30})
    assert out == {"BTC/USD": 0.50, "ETH/USD": 0.0, "SOL/USD": 0.30}
    assert rules(ev) == [("reject", "shorts_not_allowed", "ETH/USD"), ("clip", "max_per_stock", "BTC/USD")]


def test_c0_full_btc_allowed():
    out, ev = run("C0", {"BTC/USD": 1.0})
    assert out == {"BTC/USD": 1.0} and ev == []


def test_agent_2_one_twenty_second_each_allowed():
    w = {f"S{i:02d}": 1 / 22 for i in range(22)}
    assert run(2, w) == (w, [])


# ---------------------------------------------------------------------------
# 4. Total-short cap (pro rata)
# ---------------------------------------------------------------------------

def test_total_short_scaled_pro_rata_exact():
    # shorts 0.20 + 0.15 + 0.05 = 0.40 > 0.30 -> factor 0.75
    out, ev = run(4, {"L": 0.2, "A": -0.20, "B": -0.15, "C": -0.05})
    assert out["L"] == 0.2
    assert out["A"] == pytest.approx(-0.15, abs=1e-15)
    assert out["B"] == pytest.approx(-0.1125, abs=1e-15)
    assert out["C"] == pytest.approx(-0.0375, abs=1e-15)
    assert -sum(v for v in out.values() if v < 0) == pytest.approx(0.30, abs=1e-15)
    assert rules(ev) == [("clip", "max_total_short", s) for s in ("A", "B", "C")]
    assert json.loads(ev[0]["detail"])["factor"] == pytest.approx(0.75)


def test_agent_6_short_cap_is_50pct():
    w = {"A": 1 / 6, "B": 1 / 6, "C": 1 / 6, "X": -1 / 6, "Y": -1 / 6, "Z": -1 / 6}
    assert run(6, w) == (w, [])


def test_exactly_at_short_cap_not_clipped():
    assert run(4, {"A": -0.1, "B": -0.1, "C": -0.1})[1] == []


# ---------------------------------------------------------------------------
# 5. Gross cap (pro rata)
# ---------------------------------------------------------------------------

def test_gross_scaled_pro_rata_exact():
    # agent 5: 6 x 0.20 = 1.20 -> factor 1/1.2
    w = {s: 0.20 for s in "ABCDEF"}
    out, ev = run(5, w)
    assert all(v == pytest.approx(1 / 6, abs=1e-15) for v in out.values())
    assert sum(out.values()) == pytest.approx(1.0, abs=1e-15)
    assert rules(ev) == [("clip", "max_gross", s) for s in "ABCDEF"]


def test_gross_scales_longs_and_shorts_alike():
    # agent 4: 0.2 x 4 longs + 0.1 x 3 shorts = 1.10 -> factor 1/1.1
    w = {"A": 0.2, "B": 0.2, "C": 0.2, "D": 0.2, "X": -0.1, "Y": -0.1, "Z": -0.1}
    out, _ = run(4, w)
    for s in w:
        assert out[s] == pytest.approx(w[s] / 1.1, abs=1e-15)


def test_agent_15_150pct_gross_allowed_151pct_clipped():
    w150 = {s: 0.30 for s in "ABCDE"}                     # 1.5 (floats to 1.5000000000000002)
    assert run(15, w150) == (w150, [])
    w151 = {**{s: 0.30 for s in "ABCD"}, "E": 0.30, "F": 0.01}
    out, ev = run(15, w151)
    assert sum(abs(v) for v in out.values()) == pytest.approx(1.5, abs=1e-12)
    assert {e["rule"] for e in ev} == {"max_gross"}
    assert out["F"] == pytest.approx(0.01 * 1.5 / 1.51)


def test_agent_15_long_short_at_limits():
    w = {"A": 0.30, "B": 0.30, "C": 0.30, "D": -0.30, "E": -0.20}   # gross 1.4, short 0.5
    assert run(15, w) == (w, [])


# ---------------------------------------------------------------------------
# Order of checks
# ---------------------------------------------------------------------------

def test_order_of_checks():
    # agent 4: B rejected (hard to borrow), A cut 0.30 -> 0.20, short cut 0.40 -> 0.30,
    # then gross 0.20+0.20+0.20+0.20+0.20+0.30 = 1.30 -> scaled to 1.00
    w = {"A": 0.30, "C": 0.20, "D": 0.20, "E": 0.20, "F": 0.20,
         "B": -0.15, "X": -0.20, "Y": -0.20}
    flags = {"B": False, "X": True, "Y": True}
    out, ev = run(4, w, flags=flags)
    order = [e["rule"] for e in ev]
    assert order == ["easy_to_borrow", "max_per_stock", "max_total_short", "max_total_short"] + ["max_gross"] * 7
    assert out["B"] == 0.0
    # after the per-stock and short steps: A 0.20, X/Y -0.15; gross 1.30
    assert out["A"] == pytest.approx(0.20 / 1.3)
    assert out["X"] == pytest.approx(-0.15 / 1.3)
    assert sum(abs(v) for v in out.values()) == pytest.approx(1.0)
    # a rejected short doesn't count towards the short cap: without rejecting B first,
    # shorts would be 0.55 and X would end at a different size
    gross_ev = [e for e in ev if e["rule"] == "max_gross" and e["symbol"] == "X"][0]
    assert gross_ev["requested_weight"] == pytest.approx(-0.15)


def test_per_stock_cut_happens_before_short_cap():
    # agent 4: A -0.25 -> -0.20 first; shorts then 0.20 + 0.10 = 0.30, at the cap: no scaling
    out, ev = run(4, {"A": -0.25, "B": -0.10})
    assert out == {"A": -0.20, "B": -0.10}
    assert [e["rule"] for e in ev] == ["max_per_stock"]


def test_shorts_not_allowed_before_easy_to_borrow():
    _, ev = run(14, {"A": -0.05}, flags={})
    assert [e["rule"] for e in ev] == ["shorts_not_allowed"]


# ---------------------------------------------------------------------------
# Invariant: never larger, never flipped, never new
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("agent_id", sorted(risk.PROFILES))
def test_no_output_weight_larger_than_input(agent_id):
    rng = random.Random(f"risk-{agent_id}")
    syms = [f"S{i:02d}" for i in range(22)]
    for _ in range(400):
        chosen = rng.sample(syms, rng.randint(1, 22))
        w = {s: rng.choice([0.0, rng.uniform(-0.6, 0.6)]) for s in chosen}
        flags = {s: rng.random() < 0.7 for s in syms if rng.random() < 0.9}
        cur = {s: rng.uniform(-0.3, 0.3) for s in syms if rng.random() < 0.5}
        out, ev = run(agent_id, w, flags=flags, current=cur)
        p = risk.profile(agent_id)
        assert set(out) == set(w)
        for s in w:
            assert abs(out[s]) <= abs(w[s])
            assert out[s] == 0 or (out[s] > 0) == (w[s] > 0)
        assert sum(abs(v) for v in out.values()) <= p.max_gross + 1e-9
        assert -sum(v for v in out.values() if v < 0) <= p.max_total_short + 1e-9
        assert all(abs(v) <= p.max_per_stock + 1e-9 for v in out.values())
        for s, v in out.items():
            if v < 0 and not flags.get(s, False):
                assert -v <= max(-cur.get(s, 0.0), 0.0) + 1e-9
        changed = {s for s in w if out[s] != w[s]}
        assert changed <= {e["symbol"] for e in ev}


# ---------------------------------------------------------------------------
# Margin
# ---------------------------------------------------------------------------

def test_margin_trigger_at_29_9_not_at_30():
    pos = {"A": 60.0, "B": -40.0}                         # gross 100
    assert margin_check(30.0, 100.0, pos) == (None, [])
    out, ev = margin_check(29.9, 100.0, pos)
    assert out is not None and {e["rule"] for e in ev} == {"margin"}
    assert {e["event_type"] for e in ev} == {"forced_reduction"}


def test_margin_scales_to_exactly_40pct():
    pos = {"A": 200.0, "B": -100.0, "C": 0.0}             # gross 300, equity 60 = 20%
    out, ev = margin_check(60.0, 300.0, pos)
    # new gross = 60 / 0.4 = 150 -> factor 0.5; weights = scaled value / equity
    assert out["A"] == pytest.approx(100.0 / 60.0)
    assert out["B"] == pytest.approx(-50.0 / 60.0)
    assert out["C"] == 0.0
    new_gross = sum(abs(w) for w in out.values()) * 60.0
    assert 60.0 / new_gross == pytest.approx(0.40)
    assert [e["symbol"] for e in ev] == ["A", "B"]
    assert ev[0]["requested_weight"] == pytest.approx(200 / 60)
    assert json.loads(ev[0]["detail"])["factor"] == pytest.approx(0.5)


def test_margin_never_increases_or_flips():
    pos = {"A": 500.0, "B": -300.0, "C": 50.0}
    out, _ = margin_check(100.0, 850.0, pos)
    for s, v in pos.items():
        assert abs(out[s] * 100.0) <= abs(v) and (out[s] > 0) == (v > 0)


@pytest.mark.parametrize("equity", [0.0, -50.0])
def test_margin_equity_zero_or_negative_closes_everything(equity):
    out, ev = margin_check(equity, 100.0, {"A": 60.0, "B": -40.0})
    assert out == {"A": 0.0, "B": 0.0}
    assert [e["applied_weight"] for e in ev] == [0.0, 0.0]


def test_margin_no_positions_no_breach():
    assert margin_check(0.0, 0.0, {}) == (None, [])
    assert margin_check(100.0, 0.0, {"A": 0.0}) == (None, [])


def test_margin_gross_must_match_positions():
    with pytest.raises(ValueError):
        margin_check(10.0, 90.0, {"A": 60.0, "B": -40.0})


# ---------------------------------------------------------------------------
# Inputs, purity, events fit the table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "0.1", None])
def test_bad_current_weight_rejected(bad):
    with pytest.raises(ValueError):
        run(4, {"A": -0.1}, current={"A": bad})


def test_risk_module_imports_nothing_external():
    tree = ast.parse((ROOT / "execution" / "risk.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported <= {"__future__", "json", "math", "dataclasses", "types", "typing"}


def test_runs_sealed_and_does_not_change_inputs(monkeypatch):
    w = {"A": 0.30, "B": -0.40, "C": 0.5}
    flags, cur = {"B": False}, {"B": -0.1}
    d = Decision(w, "test")
    expected = apply_risk(4, d, flags, cur), margin_check(10.0, 120.0, {"A": 100.0, "B": -20.0})
    sealed(monkeypatch)
    got = apply_risk(4, d, flags, cur), margin_check(10.0, 120.0, {"A": 100.0, "B": -20.0})
    monkeypatch.undo()
    assert got == expected
    assert dict(d.weights) == w and flags == {"B": False} and cur == {"B": -0.1}


def test_events_insert_into_risk_events_table():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript((ROOT / "data" / "schema.sql").read_text())
    _, ev1 = run(4, {"A": 0.30, "B": -0.40, "C": 0.5, "D": -0.1}, flags={"B": True})
    _, ev2 = margin_check(29.9, 100.0, {"A": 60.0, "B": -40.0})
    _, ev3 = margin_check(-1.0, 100.0, {"A": 60.0, "B": -40.0})
    for e in ev1 + ev2 + ev3:
        conn.execute("INSERT INTO risk_events (agent_id, event_type, rule, symbol, requested_weight, "
                     "applied_weight, detail) VALUES (:agent_id, :event_type, :rule, :symbol, "
                     ":requested_weight, :applied_weight, :detail)", {"agent_id": "4", **e})
    n = conn.execute("SELECT count(*) FROM risk_events").fetchone()[0]
    assert n == len(ev1 + ev2 + ev3) > 0
    assert set(ev1[0]) == {"event_type", "rule", "symbol", "requested_weight", "applied_weight", "detail"}
