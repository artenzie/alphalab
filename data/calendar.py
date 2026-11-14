"""Market calendar: NYSE trading days, closes, and the daily run times.

The scheduler calls these to decide whether anything runs today and when:

    from data import calendar as cal
    cal.is_trading_day(date(2026, 11, 27))   # True
    cal.session_close(date(2026, 11, 27))    # 2026-11-27 18:00 UTC (13:00 ET early close)
    cal.equity_times(date(2026, 11, 27))     # close +1:30 / +1:45 / +2:00 (protocol §3)
    cal.crypto_times(date(2026, 11, 27))     # 00:05Z / 00:10Z / 00:15Z on 28 Nov (§13)

Every datetime returned is timezone-aware UTC. ET is converted with the
America/New_York zone (and UK display times with Europe/London), so the US
and UK clock changes, which fall on different dates in March and Oct/Nov,
are handled by the tz database rather than by fixed offsets.

Source: Alpaca's trading calendar, cached in the database (calendar_fetches +
market_sessions, both append-only). Lookups read the cache only; the network
is used by `refresh()` (the daily job) or, once, on a date no fetch covers.
So a network blip doesn't stop the daily run as long as the cache covers the
date; if neither the cache nor Alpaca can answer, CalendarUnavailable is
raised and nothing is guessed.

For a date D the answer comes from the newest fetch whose range contains D:
a session row means open, no row means closed (weekend, holiday, or a
closure announced after an earlier fetch).

Usage:
    python -m data.calendar --refresh     # fetch the default range into the cache

(Run it with -m, not as data/calendar.py: run by path, this file would shadow
the standard library's `calendar` module.)
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

from data import db

NY = ZoneInfo("America/New_York")
UK = ZoneInfo("Europe/London")

# Protocol §3: offsets from the (possibly early) close.
BARS_FETCH_AFTER_CLOSE = timedelta(hours=1, minutes=30)
SNAPSHOT_AFTER_CLOSE = timedelta(hours=1, minutes=45)
DECIDE_AFTER_CLOSE = timedelta(hours=2)

# Protocol §13 (v1.1): offsets from the 00:00 UTC crypto bar close.
CRYPTO_BARS_FETCH_AFTER = timedelta(minutes=5)
CRYPTO_SNAPSHOT_AFTER = timedelta(minutes=10)
CRYPTO_DECIDE_AFTER = timedelta(minutes=15)

# Default cache range: from the price backfill start to the end of next year.
DEFAULT_START = date(2024, 11, 1)


def default_end(today: date | None = None) -> date:
    today = today or datetime.now(timezone.utc).date()
    return date(today.year + 1, 12, 31)


class CalendarError(Exception):
    """Base class for calendar errors."""


class NotATradingDay(CalendarError):
    """The market is closed on that date."""


class CalendarUnavailable(CalendarError):
    """No cached answer for the date, and Alpaca couldn't be reached."""


@dataclass(frozen=True)
class DailyTimes:
    """When one day's steps run. All fields are UTC."""

    close: datetime          # equity: session close; crypto: the daily bar's close (00:00Z)
    bars_fetch_at: datetime
    snapshot_at: datetime
    decide_at: datetime

    def as_dict(self) -> dict[str, datetime]:
        return {
            "close": self.close,
            "bars_fetch_at": self.bars_fetch_at,
            "snapshot_at": self.snapshot_at,
            "decide_at": self.decide_at,
        }


@dataclass(frozen=True)
class Session:
    session_date: date
    open: datetime   # UTC
    close: datetime  # UTC


# ---------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------

def et_to_utc(d: date, t: dtime) -> datetime:
    """Wall-clock time in New York on date d, as UTC."""
    return datetime.combine(d, t, tzinfo=NY).astimezone(timezone.utc)


def to_et(ts: datetime) -> datetime:
    return _require_aware(ts).astimezone(NY)


def to_uk(ts: datetime) -> datetime:
    return _require_aware(ts).astimezone(UK)


def _require_aware(ts: datetime) -> datetime:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("naive datetime: attach a time zone first")
    return ts


def _alpaca_time(d: date, v: Any) -> datetime:
    """Alpaca's open/close as UTC. alpaca-py gives naive ET datetimes; strings are 'HH:MM'."""
    if isinstance(v, datetime):
        if v.tzinfo is None:
            return et_to_utc(d, v.time())
        return v.astimezone(timezone.utc)
    if isinstance(v, dtime):
        return et_to_utc(d, v)
    return et_to_utc(d, dtime.fromisoformat(str(v)))


def _as_date(v: Any) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

@contextmanager
def _connection(conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
    if conn is not None:
        yield conn
        return
    c = db.init_db()
    try:
        yield c
    finally:
        c.close()


def _covering_fetch(conn: sqlite3.Connection, d: date) -> int | None:
    row = conn.execute(
        """SELECT fetch_id FROM calendar_fetches
           WHERE range_start <= :d AND range_end >= :d
           ORDER BY fetched_at DESC, fetch_id DESC LIMIT 1""",
        {"d": d.isoformat()},
    ).fetchone()
    return None if row is None else row[0]


def _cached(conn: sqlite3.Connection, d: date) -> tuple[bool, Session | None]:
    """(covered, session). covered=False means no fetch includes d."""
    fetch_id = _covering_fetch(conn, d)
    if fetch_id is None:
        return False, None
    row = conn.execute(
        "SELECT open_ts, close_ts FROM market_sessions WHERE fetch_id = ? AND session_date = ?",
        (fetch_id, d.isoformat()),
    ).fetchone()
    if row is None:
        return True, None
    return True, Session(d, _parse_ts(row["open_ts"]), _parse_ts(row["close_ts"]))


def _days(start: date, end: date) -> Iterator[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def refresh(
    conn: sqlite3.Connection,
    trading: Any,
    start: date = DEFAULT_START,
    end: date | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int | None:
    """Fetch Alpaca's calendar for [start, end] and cache it.

    A new fetch (with its sessions) is stored only if it changes the cached
    answer for some date in the range, so re-running is a no-op. Returns the
    new fetch_id, or None if nothing changed. Raises CalendarUnavailable if
    Alpaca can't be reached after retries.
    """
    from alpaca.trading.requests import GetCalendarRequest
    from data.ingest import IngestError, with_retries

    end = end or default_end()
    if end < start:
        raise ValueError("end before start")
    req = GetCalendarRequest(start=start, end=end)
    try:
        raw = with_retries(lambda: trading.get_calendar(req), "trading calendar", sleep)
    except IngestError as exc:
        raise CalendarUnavailable(str(exc)) from exc

    sessions: dict[date, Session] = {}
    for c in raw:
        d = _as_date(c.date)
        if start <= d <= end:
            sessions[d] = Session(d, _alpaca_time(d, c.open), _alpaca_time(d, c.close))

    if all(_cached(conn, d) == (True, sessions.get(d)) for d in _days(start, end)):
        return None

    with conn:
        fetch_id = conn.execute(
            """INSERT INTO calendar_fetches (range_start, range_end, fetched_at, n_sessions)
               VALUES (?, ?, ?, ?)""",
            (start.isoformat(), end.isoformat(), db.utc_ts(), len(sessions)),
        ).lastrowid
        conn.executemany(
            """INSERT INTO market_sessions (fetch_id, session_date, open_ts, close_ts)
               VALUES (?, ?, ?, ?)""",
            [(fetch_id, s.session_date.isoformat(), db.utc_ts(s.open), db.utc_ts(s.close))
             for s in sorted(sessions.values(), key=lambda s: s.session_date)],
        )
    return fetch_id


def _default_trading() -> Any:
    from data.ingest import IngestError, make_clients

    try:
        return make_clients().trading
    except IngestError as exc:
        raise CalendarUnavailable(str(exc)) from exc


def session(d: date, conn: sqlite3.Connection | None = None, trading: Any = None) -> Session | None:
    """The session on d, or None if the market is closed.

    Reads the cache; on a date no fetch covers, refreshes once (the default
    range, widened to include d) and reads again.
    """
    with _connection(conn) as c:
        covered, s = _cached(c, d)
        if covered:
            return s
        try:
            trading = trading if trading is not None else _default_trading()
            refresh(c, trading, min(DEFAULT_START, d), max(default_end(), d))
        except CalendarUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure here means "no answer"
            raise CalendarUnavailable(f"calendar refresh failed: {type(exc).__name__}: {exc}") from exc
        covered, s = _cached(c, d)
        if not covered:
            raise CalendarUnavailable(f"Alpaca's calendar doesn't cover {d}")
        return s


# ---------------------------------------------------------------------------
# What the scheduler calls
# ---------------------------------------------------------------------------

def is_trading_day(d: date, conn: sqlite3.Connection | None = None, trading: Any = None) -> bool:
    return session(d, conn, trading) is not None


def session_close(d: date, conn: sqlite3.Connection | None = None, trading: Any = None) -> datetime:
    """UTC close of the NYSE session on d (early closes included)."""
    s = session(d, conn, trading)
    if s is None:
        raise NotATradingDay(f"{d} is not a trading day")
    return s.close


def equity_times(d: date, conn: sqlite3.Connection | None = None, trading: Any = None) -> DailyTimes:
    """Protocol §3 times for trading day d: close +1:30 / +1:45 / +2:00, in UTC."""
    close = session_close(d, conn, trading)
    return DailyTimes(
        close=close,
        bars_fetch_at=close + BARS_FETCH_AFTER_CLOSE,
        snapshot_at=close + SNAPSHOT_AFTER_CLOSE,
        decide_at=close + DECIDE_AFTER_CLOSE,
    )


def crypto_times(bar_date: date) -> DailyTimes:
    """Protocol §13 times for the crypto daily bar of bar_date.

    That bar closes at 00:00 UTC on bar_date + 1; the steps run 5 / 10 / 15
    minutes later. Every calendar day counts, and no time zone but UTC is involved.
    """
    close = datetime.combine(bar_date + timedelta(days=1), dtime(0, 0), tzinfo=timezone.utc)
    return DailyTimes(
        close=close,
        bars_fetch_at=close + CRYPTO_BARS_FETCH_AFTER,
        snapshot_at=close + CRYPTO_SNAPSHOT_AFTER,
        decide_at=close + CRYPTO_DECIDE_AFTER,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fmt(ts: datetime) -> str:
    return (f"{ts:%Y-%m-%d %H:%M}Z  (ET {to_et(ts):%H:%M %Z}, UK {to_uk(ts):%H:%M %Z})")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--refresh", action="store_true", help="fetch the default range into the cache")
    p.add_argument("--show", nargs="*", type=date.fromisoformat, default=[], metavar="YYYY-MM-DD",
                   help="print the run times for these dates")
    p.add_argument("--db", default=str(db.DEFAULT_DB_PATH), help="database path")
    args = p.parse_args(argv)
    conn = db.init_db(args.db)
    try:
        if args.refresh:
            try:
                fid = refresh(conn, _default_trading())
            except CalendarUnavailable as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            print(f"Cached {DEFAULT_START}..{default_end()}: "
                  + ("unchanged" if fid is None else f"new fetch {fid}"))
        for d in args.show:
            s = session(d, conn)
            if s is None:
                print(f"{d}  closed")
                continue
            t = equity_times(d, conn)
            print(f"{d}  close {_fmt(t.close)}\n"
                  f"            decide {_fmt(t.decide_at)}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
