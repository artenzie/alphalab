"""Schema tests: the database builds, is append-only, and reads prices point-in-time."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data import db  # noqa: E402

CUTOFF = "2026-11-16T22:45:00.000Z"
DECIDED = "2026-11-16T23:00:00.000Z"
FILLED = "2026-11-17T14:30:00.000Z"


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "test.db")
    yield c
    c.close()


def add_price(conn, symbol="AAPL", bar_date="2026-11-16", close=200.0,
              adj_close=200.0, fetched_at="2026-11-16T22:30:00.000Z"):
    conn.execute(
        """INSERT INTO prices (symbol, asset_class, bar_date, bar_ts,
               open, high, low, close, volume,
               adj_open, adj_high, adj_low, adj_close, adj_volume, feed, fetched_at)
           VALUES (?, 'us_equity', ?, ?, ?, ?, ?, ?, 1000, ?, ?, ?, ?, 1000, 'iex', ?)""",
        (symbol, bar_date, f"{bar_date}T05:00:00.000Z",
         close, close, close, close, adj_close, adj_close, adj_close, adj_close, fetched_at),
    )


def add_snapshot(conn, asset_class="us_equity", date="2026-11-16"):
    return conn.execute(
        """INSERT INTO snapshots (asset_class, snapshot_date, cutoff_ts, built_at,
               payload, sha256, checks, checks_passed)
           VALUES (?, ?, ?, ?, '{}', ?, '{}', 1)""",
        (asset_class, date, CUTOFF, CUTOFF, "0" * 64),
    ).lastrowid


def add_decision(conn, snapshot_id, agent_id="8", decided_at=DECIDED, status="ok"):
    return conn.execute(
        """INSERT INTO decisions (agent_id, snapshot_id, decided_at, status,
               model_id, provider_served, reasoning_setting, prompt_hash,
               raw_response, parsed_targets, reasons, tokens_in, tokens_out,
               cost_usd, latency_ms)
           VALUES (?, ?, ?, ?, 'anthropic/claude-sonnet-5.5', 'Anthropic', 'default',
               ?, '{"targets": {"AAPL": 0.1}}', ?, ?, 1200, 300, 0.004, 2100)""",
        (agent_id, snapshot_id, decided_at, status, "a" * 64,
         json.dumps({"AAPL": 0.1}), json.dumps({"AAPL": "trend up"})),
    ).lastrowid


@pytest.fixture
def populated(conn):
    """One row in every append-only table."""
    add_price(conn)
    snap = add_snapshot(conn)
    dec = add_decision(conn, snap)
    conn.execute(
        """INSERT INTO risk_events (agent_id, decision_id, event_type, rule, symbol,
               requested_weight, applied_weight) VALUES ('8', ?, 'clip', 'max_per_stock', 'AAPL', 0.25, 0.20)""",
        (dec,),
    )
    order = conn.execute(
        """INSERT INTO orders (agent_id, decision_id, origin, venue, symbol, asset_class,
               side, qty, target_weight) VALUES ('8', ?, 'decision', 'ledger', 'AAPL', 'us_equity', 'buy', 50, 0.1)""",
        (dec,),
    ).lastrowid
    conn.execute(
        """INSERT INTO fills (order_id, status, filled_at, qty, reference_price,
               slippage_bps, fill_price, fee_usd) VALUES (?, 'filled', ?, 50, 201.0, 5, 201.1005, 0)""",
        (order, FILLED),
    )
    conn.execute(
        """INSERT INTO positions (agent_id, as_of_date, symbol, asset_class, qty, avg_cost,
               mark_price, market_value, unrealised_pnl)
           VALUES ('8', '2026-11-17', 'AAPL', 'us_equity', 50, 201.1005, 202.0, 10100, 44.975)""",
    )
    conn.execute(
        """INSERT INTO equity (agent_id, as_of_date, cash, long_value, equity, gross_exposure)
           VALUES ('8', '2026-11-17', 89944.975, 10100, 100044.975, 10100)""",
    )
    conn.execute(
        """INSERT INTO mirror_recon (agent_id, as_of_date, alpaca_account, ledger_equity,
               alpaca_equity, difference_usd) VALUES ('8', '2026-11-17', 2, 100044.975, 100050.0, 5.025)""",
    )
    conn.execute("INSERT INTO heartbeat (run_id, job, event) VALUES ('r1', 'decide', 'started')")
    conn.commit()
    return conn


# --- builds -----------------------------------------------------------------

def test_database_builds(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(db.ALL_TABLES) <= tables
    triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    for t in db.APPEND_ONLY_TABLES:
        assert f"{t}_no_update" in triggers and f"{t}_no_delete" in triggers
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_init_is_idempotent(tmp_path):
    path = tmp_path / "twice.db"
    db.init_db(path).close()
    c = db.init_db(path)
    assert c.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 17
    c.close()


def test_agents_cover_equity_and_crypto(conn):
    rows = dict(conn.execute("SELECT agent_id, asset_class FROM agents").fetchall())
    assert all(rows[str(i)] == "us_equity" for i in range(1, 16))
    assert rows["C0"] == rows["C1"] == "crypto"


# --- append-only --------------------------------------------------------------

def test_update_on_decisions_rejected(populated):
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        populated.execute("UPDATE decisions SET status = 'invalid'")


def test_delete_on_decisions_rejected(populated):
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        populated.execute("DELETE FROM decisions")
    assert populated.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1


@pytest.mark.parametrize("table", db.APPEND_ONLY_TABLES)
def test_every_data_table_is_append_only(populated, table):
    pk = populated.execute(f"SELECT name FROM pragma_table_info('{table}') WHERE pk = 1").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        populated.execute(f"UPDATE {table} SET {pk} = {pk}")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        populated.execute(f"DELETE FROM {table}")


def test_agents_table_stays_editable(conn):
    conn.execute("UPDATE agents SET name = name WHERE agent_id = '1'")


def test_correction_is_a_new_row(populated):
    original = populated.execute("SELECT decision_id, snapshot_id FROM decisions").fetchone()
    populated.execute(
        """INSERT INTO decisions (agent_id, snapshot_id, decided_at, status, parsed_targets, corrects_id)
           VALUES ('8', ?, ?, 'ok', '{"AAPL": 0.1}', ?)""",
        (original["snapshot_id"], "2026-11-17T09:00:00.000Z", original["decision_id"]),
    )
    assert populated.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 2


# --- constraints ---------------------------------------------------------------

@pytest.mark.parametrize("bad", ["2026-11-16 23:00:00", "2026-11-16T23:00:00+00:00",
                                 "2026-11-16T23:00:00Z", "2026-11-16T18:00:00.000-05:00"])
def test_non_utc_timestamp_rejected(conn, bad):
    snap = add_snapshot(conn)
    with pytest.raises(sqlite3.IntegrityError):
        add_decision(conn, snap, decided_at=bad)


def test_default_timestamp_format(conn):
    conn.execute("INSERT INTO heartbeat (run_id, job, event) VALUES ('r', 'j', 'started')")
    at = conn.execute("SELECT at FROM heartbeat").fetchone()[0]
    assert at.endswith("Z") and len(at) == 24


def test_utc_ts_helper():
    from datetime import datetime, timedelta, timezone
    est = timezone(timedelta(hours=-5))
    assert db.utc_ts(datetime(2026, 11, 16, 17, 45, tzinfo=est)) == "2026-11-16T22:45:00.000Z"
    with pytest.raises(ValueError):
        db.utc_ts(datetime(2026, 11, 16, 17, 45))


def test_bad_status_rejected(conn):
    snap = add_snapshot(conn)
    with pytest.raises(sqlite3.IntegrityError):
        add_decision(conn, snap, status="held")


@pytest.mark.parametrize("status", ["ok", "invalid", "unavailable", "capped"])
def test_valid_statuses_accepted(conn, status):
    add_decision(conn, add_snapshot(conn), status=status)


def test_decision_before_snapshot_cutoff_rejected(conn):
    snap = add_snapshot(conn)
    with pytest.raises(sqlite3.IntegrityError, match="cutoff"):
        add_decision(conn, snap, decided_at="2026-11-16T22:44:59.999Z")


def test_crypto_agent_cannot_use_equity_snapshot(conn):
    snap = add_snapshot(conn, asset_class="us_equity")
    with pytest.raises(sqlite3.IntegrityError, match="asset class"):
        add_decision(conn, snap, agent_id="C1")


# --- point-in-time prices -------------------------------------------------------

def test_prices_as_of_returns_version_known_at_cutoff(conn):
    # Version 1, fetched the evening of the bar.
    add_price(conn, close=400.0, adj_close=400.0, fetched_at="2026-11-16T22:30:00.000Z")
    # Version 2, fetched after a 2-for-1 split: same raw bar, adjusted close halved.
    add_price(conn, close=400.0, adj_close=200.0, fetched_at="2026-11-20T22:30:00.000Z")
    conn.commit()

    before = db.prices_as_of(conn, "2026-11-16T22:45:00.000Z")
    assert len(before) == 1
    assert before[0]["adj_close"] == 400.0
    assert before[0]["fetched_at"] == "2026-11-16T22:30:00.000Z"

    after = db.prices_as_of(conn, "2026-11-20T22:45:00.000Z")
    assert len(after) == 1
    assert after[0]["adj_close"] == 200.0
    assert after[0]["close"] == 400.0  # raw bar unchanged


def test_prices_as_of_hides_bars_not_yet_fetched_or_in_future(conn):
    add_price(conn, bar_date="2026-11-16", fetched_at="2026-11-16T22:30:00.000Z")
    add_price(conn, bar_date="2026-11-17", fetched_at="2026-11-17T22:30:00.000Z")
    add_price(conn, symbol="SPY", bar_date="2026-11-16", fetched_at="2026-11-16T22:30:00.000Z")
    rows = db.prices_as_of(conn, "2026-11-16T22:45:00.000Z")
    assert {(r["symbol"], r["bar_date"]) for r in rows} == {("AAPL", "2026-11-16"), ("SPY", "2026-11-16")}
    only_aapl = db.prices_as_of(conn, "2026-11-17T22:45:00.000Z", symbols=["AAPL"])
    assert [r["bar_date"] for r in only_aapl] == ["2026-11-16", "2026-11-17"]


def test_same_fetch_cannot_insert_a_bar_twice(conn):
    add_price(conn)
    with pytest.raises(sqlite3.IntegrityError):
        add_price(conn)
