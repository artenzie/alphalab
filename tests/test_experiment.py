"""Experiment databases (protocol §9, v1.13): runner/experiment.py."""

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from data import db  # noqa: E402
from execution import ledger as L  # noqa: E402
from execution import ledger_db as LD  # noqa: E402
from runner import experiment as ex  # noqa: E402
from test_schema import add_fill_bar, add_fill_window  # noqa: E402
from test_snapshot import close_of, make_db  # noqa: E402

MON = date(2026, 11, 16)
END = date(2027, 1, 8)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "src.db"
    c = make_db(path, eq_end=MON, cr_end=MON)
    with c:
        add_fill_bar(c)
        add_fill_window(c)
    c.close()
    return path


def counts(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in db.ALL_TABLES}
    finally:
        c.close()


def test_create_copies_market_data_and_no_ledger(source, tmp_path):
    before = counts(source)
    out = tmp_path / "dev.db"
    exp = ex.create("dev", MON, source, out, END)
    assert exp == ex.Experiment("dev", MON, "src.db", END)
    after = counts(out)
    for t in ex.MARKET_TABLES:
        assert after[t] == before[t] > 0, t
    for t in ("snapshots", "decisions", "orders", "fills", "positions", "equity", "risk_events", "heartbeat"):
        assert after[t] == 0, t
    assert counts(source) == before                        # source untouched
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(out))
    q = "SELECT * FROM prices ORDER BY price_id"
    assert src.execute(q).fetchall() == dst.execute(q).fetchall()   # ids and fetched_at kept
    src.close()
    dst.close()


def test_day_one_marks_every_agent_at_100k(source, tmp_path):
    out = tmp_path / "official.db"
    ex.create("official", ex.OFFICIAL_START, source, out, ex.OFFICIAL_END)
    conn = db.connect(out)
    acct = LD.load_account(conn, "8", before=ex.OFFICIAL_START)
    assert acct.cash == L.START_CASH and not acct.positions
    conn.close()


def test_never_overwrites_and_official_start_is_fixed(source, tmp_path):
    out = tmp_path / "x.db"
    ex.create("dev", MON, source, out, END)
    with pytest.raises(ex.ExperimentError, match="never overwritten"):
        ex.create("dev", MON, source, out, END)
    with pytest.raises(ex.ExperimentError, match="starts on 2027-01-11"):
        ex.create("official", date(2027, 1, 4), source, tmp_path / "o.db", ex.OFFICIAL_END)
    assert not (tmp_path / "o.db").exists()


def test_require_checks_label_and_start(tmp_path, source):
    bare = db.init_db(tmp_path / "bare.db")
    with pytest.raises(ex.ExperimentError, match="no experiment label"):
        ex.require(bare)
    bare.close()
    ex.create("shakedown", ex.SHAKEDOWN_START, source, tmp_path / "s.db", ex.SHAKEDOWN_END)
    conn = db.connect(tmp_path / "s.db")
    assert ex.require(conn, ex.SHAKEDOWN_START).name == "shakedown"
    with pytest.raises(ex.ExperimentError, match="before the shakedown experiment's start"):
        ex.require(conn, ex.SHAKEDOWN_START - timedelta(days=1))
    conn.close()


def test_cli_create_and_show(source, tmp_path, capsys):
    out = tmp_path / "cli.db"
    assert ex.main(["create", "dev", "--start", "2026-11-16", "--end", "2027-01-08", "--from", str(source), "--db", str(out)]) == 0
    assert ex.main(["show", "--db", str(out)]) == 0
    assert "experiment 'dev', 2026-11-16 to 2027-01-08" in capsys.readouterr().out


def test_end_date_rules(source, tmp_path):
    with pytest.raises(ex.ExperimentError, match="needs an end date"):
        ex.create("dev", MON, source, tmp_path / "a.db")
    with pytest.raises(ex.ExperimentError, match="before the start date"):
        ex.create("dev", MON, source, tmp_path / "b.db", MON - timedelta(days=1))
    with pytest.raises(ex.ExperimentError, match="2027-01-11 to 2027-04-02"):
        ex.create("official", ex.OFFICIAL_START, source, tmp_path / "c.db", date(2027, 4, 1))
    with pytest.raises(ex.ExperimentError, match="2026-12-14 to 2027-01-08"):
        ex.create("shakedown", ex.SHAKEDOWN_START, source, tmp_path / "d.db", date(2027, 1, 7))
    with pytest.raises(ex.ExperimentError, match="2026-12-14 to 2027-01-08"):
        ex.create("shakedown", MON, source, tmp_path / "e.db", ex.SHAKEDOWN_END)
    for f in "abcde":
        assert not (tmp_path / f"{f}.db").exists()
    with pytest.raises(SystemExit):                                     # --end is required on the CLI
        ex.main(["create", "dev", "--start", "2026-11-16", "--from", str(source), "--db", str(tmp_path / "f.db")])


def test_label_from_before_v1_15_reads_end_date_none(tmp_path):
    conn = db.init_db(tmp_path / "old.db")
    with conn:
        conn.execute("INSERT INTO experiment (name, start_date) VALUES ('dev', '2026-11-16')")
    assert ex.get(conn) == ex.Experiment("dev", MON, None, None)
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("UPDATE experiment SET end_date = '2027-01-08'")      # append-only: stays None
    conn.close()
