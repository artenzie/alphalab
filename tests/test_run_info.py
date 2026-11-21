"""input.run (v1.15): the run's end date and trading days left, from the label and the calendar."""

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from data import calendar as cal  # noqa: E402
from data import db  # noqa: E402
from runner import daily  # noqa: E402
from runner import experiment as ex  # noqa: E402
from test_snapshot import seed_calendar  # noqa: E402

REAL_DB = ROOT / "data" / "alphalab.db"
HOLIDAYS_2027 = {date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26)}


def no_network():
    raise RuntimeError("no network in tests")


@pytest.mark.skipif(not REAL_DB.exists(), reason="no real database")
def test_official_equity_against_the_cached_nyse_calendar():
    conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        run = daily.run_sessions(conn, "us_equity", ex.OFFICIAL_START, ex.OFFICIAL_END, no_network)
        assert len(run) == 57 and run[0] == ex.OFFICIAL_START and run[-1] == ex.OFFICIAL_END
        assert not HOLIDAYS_2027 & set(run)
        for i, d in enumerate(run):
            left = daily.trading_days_left(conn, "us_equity", d, ex.OFFICIAL_END, no_network)
            assert left == 57 - i - 1, d
        assert daily.trading_days_left(conn, "us_equity", ex.OFFICIAL_START, ex.OFFICIAL_END, no_network) == 56
        # the Friday before MLK Day: 18 Jan is skipped
        fri = date(2027, 1, 15)
        assert (daily.trading_days_left(conn, "us_equity", fri, ex.OFFICIAL_END, no_network)
                == daily.trading_days_left(conn, "us_equity", date(2027, 1, 19), ex.OFFICIAL_END, no_network) + 1)
        shake = daily.run_sessions(conn, "us_equity", ex.SHAKEDOWN_START, ex.SHAKEDOWN_END, no_network)
        assert daily.trading_days_left(conn, "us_equity", ex.SHAKEDOWN_START, ex.SHAKEDOWN_END,
                                       no_network) == len(shake) - 1
    finally:
        conn.close()


def test_crypto_counts_calendar_days(tmp_path):
    conn = db.init_db(tmp_path / "c.db")
    assert daily.trading_days_left(conn, "crypto", ex.OFFICIAL_START, ex.OFFICIAL_END, no_network) == 81
    assert daily.trading_days_left(conn, "crypto", ex.SHAKEDOWN_START, ex.SHAKEDOWN_END, no_network) == 25
    assert daily.trading_days_left(conn, "crypto", ex.OFFICIAL_END, ex.OFFICIAL_END, no_network) == 0
    conn.close()


def test_synthetic_calendar_and_errors(tmp_path):
    conn = db.init_db(tmp_path / "e.db")
    seed_calendar(conn)                         # weekdays, Thanksgiving and Christmas 2026 closed
    mon, end = date(2026, 11, 23), date(2026, 11, 30)
    # Tue 24, Wed 25, Fri 27, Mon 30 (Thu 26 is Thanksgiving)
    assert daily.trading_days_left(conn, "us_equity", mon, end, no_network) == 4
    assert daily.trading_days_left(conn, "us_equity", end, end, no_network) == 0
    with pytest.raises(ex.ExperimentError, match="after the experiment's end date"):
        daily.trading_days_left(conn, "us_equity", end + timedelta(days=1), end, no_network)
    with pytest.raises(ex.ExperimentError, match="no end date"):
        daily.trading_days_left(conn, "us_equity", mon, None, no_network)
    assert cal.session(mon, conn, no_network) is not None
    conn.close()
