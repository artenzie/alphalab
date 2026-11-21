"""The dry-run replay database (scripts/dry_run.py): on-time fetch stamps, rule 1, dev label only."""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dry_run  # noqa: E402
from data import calendar as cal  # noqa: E402
from data import db  # noqa: E402

import test_runner as TR  # noqa: E402

LATE_FETCH = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)      # "backfilled" long after the days
DAY1, DAY2 = date(2026, 11, 16), date(2026, 11, 17)


@pytest.fixture
def source(tmp_path):
    """Market data fetched after the fact, like alphalab.db: every bar stamped LATE_FETCH."""
    path = tmp_path / "src.db"
    c = db.init_db(path)
    TR.seed_calendar(c)
    rows = [TR.bar_row(s, d, TR.eq_close(s, d), LATE_FETCH) for d in TR.EQ_HIST if d <= DAY2 for s in TR.UNIVERSE]
    d = TR.CR_FIRST
    while d <= DAY2:
        rows += [TR.bar_row(s, d, TR.cr_close(s, d), LATE_FETCH) for s in TR.CRYPTO_SYMBOLS]
        d += timedelta(days=1)
    TR.insert_rows(c, rows)
    with c:
        ws = cal.crypto_times(DAY1).decide_at
        for s in TR.CRYPTO_SYMBOLS:
            for m in range(60):
                o = TR.cr_close(s, DAY2) * (1 + 0.0001 * m)
                c.execute("""INSERT INTO crypto_fill_bars (symbol, decision_date, window_start, bar_ts, open, high,
                             low, close, volume, feed, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'crypto', ?)""",
                          (s, DAY1.isoformat(), db.utc_ts(ws), db.utc_ts(ws + timedelta(minutes=m)), o, o, o, o,
                           db.utc_ts(LATE_FETCH)))
    c.close()
    return path


def test_build_stamps_every_bar_at_its_protocol_fetch_time(source, tmp_path):
    out = tmp_path / "dry.db"
    exp = dry_run.build(source, out, DAY1, date(2027, 1, 8))
    assert (exp.name, exp.start_date, exp.end_date) == ("dev", DAY1, date(2027, 1, 8))
    conn = db.connect(out)
    for r in conn.execute("SELECT asset_class, bar_date, bar_ts, fetched_at FROM prices"):
        d = date.fromisoformat(r["bar_date"])
        fetched = cal._parse_ts(r["fetched_at"])
        if r["asset_class"] == "crypto":
            closed = cal._parse_ts(r["bar_ts"]) + timedelta(hours=24)
            assert fetched == cal.crypto_times(d).bars_fetch_at
        else:
            closed = cal.session(d, conn, dry_run._no_network).close
            assert fetched == cal.equity_times(d, conn, dry_run._no_network).bars_fetch_at
        assert fetched >= closed, r["bar_date"]                    # never before the bar closed
    for r in conn.execute("SELECT window_start, bar_ts, fetched_at FROM crypto_fill_bars"):
        assert cal._parse_ts(r["fetched_at"]) == cal._parse_ts(r["window_start"]) + timedelta(minutes=60)
        assert cal._parse_ts(r["fetched_at"]) >= cal._parse_ts(r["bar_ts"]) + timedelta(minutes=1)
    assert conn.execute("SELECT COUNT(*) FROM crypto_fill_windows").fetchone()[0] == 1
    src = db.connect(source)
    n = src.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == n
    src.close()
    conn.close()
    with pytest.raises(dry_run.DryRunError, match="already exists"):
        dry_run.build(source, out, DAY1, date(2027, 1, 8))


def test_replayed_days_see_no_bar_after_the_decision_date(source, tmp_path):
    out = tmp_path / "dry.db"
    dry_run.build(source, out, DAY1, date(2027, 1, 8))
    record = tmp_path / "prompts"
    flags = {s: True for s in TR.UNIVERSE}
    for ac in ("equity", "crypto"):
        for d in (DAY1, DAY2):
            rep = dry_run.run_one(out, ac, d, record, flags, client_factory=TR.Client)
            assert rep.exit_code == 0 and rep.checks_passed and not rep.late, (ac, d, rep.errors)
    conn = db.connect(out)
    for r in conn.execute("SELECT snapshot_date, payload FROM snapshots"):
        p = json.loads(r["payload"])
        assert p["mode"] == "live" and not p["late"]
        assert max(f["last_date"] for f in p["symbols"].values()) == r["snapshot_date"]
        assert all(x <= r["snapshot_date"] for f in p["symbols"].values() for x in f["dates_20"])
    # day 1's orders filled at day 2 (equity open, crypto first minute bar after t_ref)
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM heartbeat WHERE event = 'error'").fetchone()[0] == 0
    prompts = sorted(p.name for p in record.glob("equity_2026-11-16_8_*.txt"))
    assert prompts == ["equity_2026-11-16_8_1.txt"]
    conn.close()


@pytest.mark.parametrize("name,start,end", [("official", "2027-01-11", "2027-04-02"),
                                            ("shakedown", "2026-12-14", "2027-01-08")])
def test_refused_on_official_and_shakedown(tmp_path, name, start, end):
    path = tmp_path / f"{name}.db"
    c = db.init_db(path)
    with c:
        c.execute("INSERT INTO experiment (name, start_date, end_date) VALUES (?, ?, ?)", (name, start, end))
    c.close()
    with pytest.raises(dry_run.DryRunError, match="only runs on a 'dev' database"):
        dry_run.run_one(path, "equity", date.fromisoformat(start), tmp_path / "p", {}, client_factory=TR.Client)
