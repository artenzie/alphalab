"""Agents 3-6, the indicator strategies (protocol §5, v1.3), on hand-made snapshots.

Each snapshot sets the §4 fields directly (no bars), so every threshold, tie
and missing value is exact. The 2027 run calendar, the sealed-run isolation
and purity tests live in test_strategies.py and cover these agents through
the registry. The real-data run is at the end and skips without the database.
"""

import math
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from data.snapshot import Snapshot, canonical_json, sha256  # noqa: E402
from strategies import long_short, mean_reversion, momentum, trend  # noqa: E402
from strategies.base import Portfolio  # noqa: E402
from test_strategies import RUN, RUN_START, STOCKS, ctx  # noqa: E402

FIELDS = ("last_close", "ret_1", "ret_5", "ret_21", "ret_63", "ma_20", "ma_100", "vol_20", "z_20",
          "volume_ratio_20")
EMPTY = Portfolio(cash=100_000.0, value=100_000.0)
MONDAY, TUESDAY = date(2027, 1, 25), date(2027, 1, 26)      # a normal week in the run


def snap(d: date, per_symbol: dict[str, dict], with_spy: bool = True) -> Snapshot:
    """A snapshot whose fields are exactly per_symbol (others None); SPY gets extreme values."""
    symbols = {s: {k: None for k in FIELDS} | {"last_close": 100.0, "n_bars": 252} | f
               for s, f in per_symbol.items()}
    if with_spy:   # SPY would win every rule if an agent forgot to skip it
        symbols["SPY"] = {k: None for k in FIELDS} | {"last_close": 700.0, "n_bars": 252, "ret_63": 9.9,
                                                      "z_20": -9.9, "ma_20": 800.0, "ma_100": 100.0}
    payload = {"version": 1, "asset_class": "us_equity", "decision_date": d.isoformat(), "mode": "backtest",
               "cutoff_ts": f"{d.isoformat()}T22:45:00.000Z", "history_sha256": "0" * 64, "symbols": symbols}
    text = canonical_json(payload)
    return Snapshot("us_equity", d.isoformat(), "backtest", payload["cutoff_ts"], text, sha256(text),
                    "0" * 64, {s: () for s in symbols})


def holding(**shares) -> Portfolio:
    return Portfolio(cash=50_000.0, positions=shares, value=100_000.0)


def rets(values: dict[str, float]) -> dict[str, dict]:
    return {s: {"ret_63": v} for s, v in values.items()}


def ranked_universe() -> dict[str, dict]:
    """All 22 stocks with distinct 63-day returns: STOCKS[0] best ... STOCKS[21] worst."""
    return rets({s: 0.5 - i * 0.03 for i, s in enumerate(STOCKS)})


# ---------------------------------------------------------------------------
# Agent 3 · Momentum
# ---------------------------------------------------------------------------

def test_momentum_top_5_at_20_percent():
    dec = momentum.decide(snap(MONDAY, ranked_universe()), EMPTY, ctx(MONDAY))
    assert dict(dec.weights) == {s: 0.20 for s in STOCKS[:5]}
    assert math.isclose(sum(dec.weights.values()), 1.0) and "SPY" not in dec.weights


def test_momentum_ties_broken_by_symbol():
    f = rets({"ZZZ": 0.3, "AAA": 0.1, "BBB": 0.1, "CCC": 0.1, "DDD": 0.1, "EEE": 0.1, "FFF": 0.1})
    dec = momentum.decide(snap(MONDAY, f), EMPTY, ctx(MONDAY))
    assert sorted(dec.weights) == ["AAA", "BBB", "CCC", "DDD", "ZZZ"]
    # order of the input doesn't matter
    rev = dict(reversed(list(f.items())))
    assert momentum.decide(snap(MONDAY, rev), EMPTY, ctx(MONDAY)) == dec


def test_momentum_weekly_trigger_and_hold():
    f = ranked_universe()
    invested = holding(**{s: 10.0 for s in STOCKS[:5]})
    assert not momentum.decide(snap(MONDAY, f), invested, ctx(MONDAY)).is_hold
    assert momentum.decide(snap(TUESDAY, f), invested, ctx(TUESDAY)).is_hold
    weeks = [d for d in RUN if not momentum.decide(snap(d, f), invested, ctx(d)).is_hold]
    assert len(weeks) == 12 and weeks[0] == RUN_START and date(2027, 1, 19) in weeks   # Tue after MLK


def test_momentum_catches_not_invested():
    f = ranked_universe()
    dec = momentum.decide(snap(TUESDAY, f), EMPTY, ctx(TUESDAY))
    assert len(dec.weights) == 5 and "missed" in dec.reason
    # SPY alone doesn't count as invested
    assert not momentum.decide(snap(TUESDAY, f), holding(SPY=10.0), ctx(TUESDAY)).is_hold


def test_momentum_missing_returns_left_out():
    f = rets({"AAA": None, "BBB": 0.2, "CCC": None, "DDD": -0.1})
    dec = momentum.decide(snap(MONDAY, f), EMPTY, ctx(MONDAY))
    assert dict(dec.weights) == {"BBB": 0.20, "DDD": 0.20}       # the rest stays in cash
    assert momentum.decide(snap(MONDAY, rets({"AAA": None})), EMPTY, ctx(MONDAY)).is_hold


# ---------------------------------------------------------------------------
# Agent 4 · Mean reversion
# ---------------------------------------------------------------------------

def mr(z: dict[str, float | None], portfolio=EMPTY, d=TUESDAY):
    return mean_reversion.decide(snap(d, {s: {"z_20": v} for s, v in z.items()}), portfolio, ctx(d))


def test_mean_reversion_entries_at_thresholds():
    dec = mr({"AAA": -1.5, "BBB": -1.49, "CCC": 1.5, "DDD": 1.49, "EEE": -3.0, "FFF": 0.0})
    assert dict(dec.weights) == {"AAA": 0.10, "CCC": -0.10, "EEE": 0.10}
    assert "SPY" not in dec.weights                       # SPY z = -9.9 is ignored


def test_mean_reversion_exits_at_zero():
    p = holding(AAA=10.0, BBB=10.0, CCC=-10.0, DDD=-10.0)
    dec = mr({"AAA": 0.0, "BBB": -0.01, "CCC": 0.0, "DDD": 0.01}, p)
    # AAA long exits at 0, CCC short exits at 0; BBB and DDD stay open at 10%
    assert dict(dec.weights) == {"BBB": 0.10, "DDD": -0.10}
    assert "exit long AAA" in dec.reason and "exit short CCC" in dec.reason


def test_mean_reversion_open_positions_keep_10_percent_when_others_change():
    p = holding(AAA=10.0, BBB=-10.0)
    dec = mr({"AAA": -0.5, "BBB": 0.7, "CCC": -2.0}, p)          # only CCC enters
    assert dict(dec.weights) == {"AAA": 0.10, "BBB": -0.10, "CCC": 0.10}


def test_mean_reversion_flips_through_zero():
    dec = mr({"AAA": 1.6, "BBB": -1.6}, holding(AAA=10.0, BBB=-10.0))
    assert dict(dec.weights) == {"AAA": -0.10, "BBB": 0.10}


def test_mean_reversion_holds_when_nothing_enters_or_exits():
    p = holding(AAA=10.0, BBB=-10.0)
    assert mr({"AAA": -1.0, "BBB": 1.0, "CCC": 1.2}, p).is_hold
    assert mr({"AAA": 0.5, "BBB": -0.5}, EMPTY).is_hold        # flat and no signal


def test_mean_reversion_day_1_always_decides():
    dec = mr({"AAA": 0.3}, EMPTY, RUN_START)
    assert not dec.is_hold and dict(dec.weights) == {}         # all cash, but a decision


def test_mean_reversion_missing_z():
    p = holding(AAA=10.0)
    # held with no z: kept; flat with no z: not entered; something else triggers the decision
    dec = mr({"AAA": None, "BBB": None, "CCC": -2.0}, p)
    assert dict(dec.weights) == {"AAA": 0.10, "CCC": 0.10}
    assert mr({"AAA": None, "BBB": None}, p).is_hold


def test_mean_reversion_daily_and_catches_not_invested():
    z = {"AAA": -2.0}
    decided = [d for d in RUN[1:] if not mr(z, EMPTY, d).is_hold]
    assert decided == RUN[1:]                                   # every day, not only weekly
    assert all(mr(z, holding(AAA=10.0), d).is_hold for d in RUN[1:])


def test_mean_reversion_no_risk_clipping():
    z = {s: -2.0 for s in STOCKS}                               # 22 longs = 220% gross
    dec = mr(z)
    assert len(dec.weights) == 22 and math.isclose(sum(dec.weights.values()), 2.2)


# ---------------------------------------------------------------------------
# Agent 5 · Trend
# ---------------------------------------------------------------------------

def tr(ma: dict[str, tuple], portfolio=EMPTY, d=TUESDAY):
    f = {s: {"ma_20": a, "ma_100": b} for s, (a, b) in ma.items()}
    return trend.decide(snap(d, f), portfolio, ctx(d))


def test_trend_equal_weight_strictly_above():
    dec = tr({"AAA": (11, 10), "BBB": (10, 10), "CCC": (9, 10), "DDD": (20, 10), "EEE": (30, 10)})
    assert sorted(dec.weights) == ["AAA", "DDD", "EEE"]
    assert all(w == 1 / 3 for w in dec.weights.values()) and "SPY" not in dec.weights


def test_trend_unchanged_set_holds():
    ma = {"AAA": (11, 10), "BBB": (9, 10), "CCC": (12, 10)}
    assert tr(ma, holding(AAA=5.0, CCC=50.0)).is_hold          # weights drifted, still hold


def test_trend_changed_set_rebalances():
    ma = {"AAA": (11, 10), "BBB": (9, 10), "CCC": (12, 10)}
    assert dict(tr(ma, holding(AAA=5.0)).weights) == {"AAA": 0.5, "CCC": 0.5}             # one added
    assert dict(tr(ma, holding(AAA=5.0, BBB=5.0, CCC=5.0)).weights) == {"AAA": 0.5, "CCC": 0.5}  # one dropped
    assert not tr(ma, holding(AAA=5.0, CCC=5.0, SPY=1.0)).is_hold                      # stray SPY
    assert not tr(ma, holding(AAA=5.0, CCC=-5.0)).is_hold                              # a short


def test_trend_empty_set():
    ma = {"AAA": (9, 10)}
    assert dict(tr(ma, holding(AAA=5.0)).weights) == {}                                # sell all
    assert tr(ma, EMPTY).is_hold                                                       # already cash
    dec = tr(ma, EMPTY, RUN_START)
    assert not dec.is_hold and dict(dec.weights) == {}                                 # day 1 decides


def test_trend_catches_not_invested_and_is_daily():
    ma = {"AAA": (11, 10)}
    assert all(dict(tr(ma, EMPTY, d).weights) == {"AAA": 1.0} for d in RUN[1:])
    assert all(tr(ma, holding(AAA=1.0), d).is_hold for d in RUN[1:])


def test_trend_missing_averages_dont_qualify():
    dec = tr({"AAA": (None, 10), "BBB": (11, None), "CCC": (None, None), "DDD": (11, 10)})
    assert dict(dec.weights) == {"DDD": 1.0}


# ---------------------------------------------------------------------------
# Agent 6 · Long-short momentum
# ---------------------------------------------------------------------------

def test_long_short_top_and_bottom_3():
    dec = long_short.decide(snap(MONDAY, ranked_universe()), EMPTY, ctx(MONDAY))
    assert dict(dec.weights) == {s: 1 / 6 for s in STOCKS[:3]} | {s: -1 / 6 for s in STOCKS[-3:]}
    assert math.isclose(sum(dec.weights.values()), 0.0, abs_tol=1e-12)
    assert math.isclose(sum(abs(w) for w in dec.weights.values()), 1.0)


def test_long_short_ties_both_sides():
    f = rets({"AAA": 0.1, "BBB": 0.1, "CCC": 0.1, "DDD": 0.1, "EEE": -0.2, "FFF": -0.2, "GGG": -0.2,
              "HHH": -0.2})
    dec = long_short.decide(snap(MONDAY, f), EMPTY, ctx(MONDAY))
    # order: AAA BBB CCC DDD | EEE FFF GGG HHH -> long AAA BBB CCC, short FFF GGG HHH
    assert sorted(s for s, w in dec.weights.items() if w > 0) == ["AAA", "BBB", "CCC"]
    assert sorted(s for s, w in dec.weights.items() if w < 0) == ["FFF", "GGG", "HHH"]


def test_long_short_weekly_and_catches_not_invested():
    f = ranked_universe()
    invested = holding(**{STOCKS[0]: 10.0, STOCKS[-1]: -10.0})
    assert long_short.decide(snap(TUESDAY, f), invested, ctx(TUESDAY)).is_hold
    assert not long_short.decide(snap(TUESDAY, f), EMPTY, ctx(TUESDAY)).is_hold
    weeks = [d for d in RUN if not long_short.decide(snap(d, f), invested, ctx(d)).is_hold]
    assert len(weeks) == 12


def test_long_short_missing_and_too_few():
    f = rets({"AAA": 0.3, "BBB": None, "CCC": 0.2, "DDD": 0.1, "EEE": 0.0, "FFF": -0.1, "GGG": -0.2})
    dec = long_short.decide(snap(MONDAY, f), EMPTY, ctx(MONDAY))
    assert "BBB" not in dec.weights and len(dec.weights) == 6
    few = rets({"AAA": 0.3, "CCC": 0.2, "DDD": 0.1, "EEE": 0.0, "FFF": -0.1, "BBB": None})
    assert long_short.decide(snap(MONDAY, few), EMPTY, ctx(MONDAY)).is_hold


# ---------------------------------------------------------------------------
# Real data: the last 57 sessions in the database, backtest snapshots
# ---------------------------------------------------------------------------

REAL_DB = ROOT / "data" / "alphalab.db"


@pytest.mark.skipif(not REAL_DB.exists(), reason="no real database")
def test_real_data_run(capsys):
    sys.path.insert(0, str(ROOT / "scripts"))
    import backtest_indicators
    report = backtest_indicators.run(REAL_DB, n_sessions=57)
    with capsys.disabled():
        print("\n" + backtest_indicators.format_report(report))
    assert len(report["sessions"]) == 57
    for agent_id, r in report["agents"].items():
        assert r["decisions"] >= 1, agent_id
        assert all("SPY" not in w for w in r["targets"]), agent_id
        assert r["orders"] == r["opens_closes"] + r["resizes"]
    assert report["agents"][3]["decisions"] == report["agents"][6]["decisions"] == report["weeks"]
