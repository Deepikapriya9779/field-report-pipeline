"""Query tests on a tiny hand-built database, so every expected number can be checked by hand.

Week of Mon 9/21:
  9/21  Keystone 8 x 8 = 64,  Volt Line 6 x 10 = 60      delays: rfi 2 (RFI 14), weather 3
  9/22  Keystone 10 x 8 = 80                              delays: weather 8, rfi 1 (RFI 9)
  9/26  (Saturday) Summit 4 x 8 = 32                      no delays
Next week:
  9/28  Keystone 5 x 8 = 40                               delays: rfi 1.5 (RFI 14)
"""

from datetime import date

import pytest

from fieldreports import db, queries
from fieldreports.loaders import SourceDoc
from fieldreports.schema import DailyReport, ExtractionMethod

WEEK = date(2026, 9, 21)
KEYSTONE = ("Keystone Interiors LLC", "drywall")
VOLT = ("Volt Line Electric, Inc.", "electrical")
SUMMIT = ("Summit Roofing & Sheet Metal", "roofing")


def _report(day, crews, delays=()):
    return DailyReport(
        project="Harbor Point Medical Office Building",
        report_date=day,
        superintendent="Dana Okafor",
        weather={"conditions": "Clear"},
        manpower=[{"company": c, "trade": t, "headcount": n, "hours": h} for (c, t), n, h in crews],
        delays=[
            {"cause": cause, "hours": hrs, "description": "x", "rfi_no": rfi}
            for cause, hrs, rfi in delays
        ],
        source_file=f"{day}.txt",
        extraction_method=ExtractionMethod.RULES,
        confidence=1.0,
    )


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "q.db")
    reports = [
        _report(
            date(2026, 9, 21),
            [(KEYSTONE, 8, 8), (VOLT, 6, 10)],
            [("rfi", 2, 14), ("weather", 3, None)],
        ),
        _report(date(2026, 9, 22), [(KEYSTONE, 10, 8)], [("weather", 8, None), ("rfi", 1, 9)]),
        _report(date(2026, 9, 26), [(SUMMIT, 4, 8)]),
        _report(date(2026, 9, 28), [(KEYSTONE, 5, 8)], [("rfi", 1.5, 14)]),
    ]
    for r in reports:
        db.save_report(conn, SourceDoc(path=r.source_file, sha256=r.source_file, text=""), r)
    return conn


def test_manhours_by_sub_for_a_week(conn):
    rows = queries.manhours(conn, "sub", WEEK)
    assert [(r["company"], r["days_on_site"], r["man_hours"]) for r in rows] == [
        ("Keystone Interiors LLC", 2, 144.0),
        ("Volt Line Electric, Inc.", 1, 60.0),
        ("Summit Roofing & Sheet Metal", 1, 32.0),  # Saturday counts in its own week
    ]


def test_manhours_by_sub_all_time(conn):
    rows = queries.manhours(conn, "sub")
    assert rows[0]["company"] == "Keystone Interiors LLC"
    assert rows[0]["man_hours"] == 184.0
    assert rows[0]["worker_days"] == 23


def test_manhours_by_trade_and_day(conn):
    by_trade = queries.manhours(conn, "trade", WEEK)
    assert {r["trade"]: r["man_hours"] for r in by_trade} == {
        "drywall": 144.0,
        "electrical": 60.0,
        "roofing": 32.0,
    }
    by_day = queries.manhours(conn, "day", WEEK)
    assert [(r["report_date"], r["headcount"], r["man_hours"]) for r in by_day] == [
        ("2026-09-21", 14, 124.0),
        ("2026-09-22", 10, 80.0),
        ("2026-09-26", 4, 32.0),
    ]


def test_delays_by_cause_for_a_week(conn):
    rows = queries.delays(conn, "cause", WEEK)
    assert rows == [
        {"cause": "weather", "events": 2, "hours": 11.0, "rfis": ""},
        {"cause": "rfi", "events": 2, "hours": 3.0, "rfis": "9, 14"},
    ]


def test_rfi_hours_all_time_answer_the_pm_question(conn):
    """'How many hours did we lose to RFIs?' -> 4.5, across RFIs 9 and 14."""
    rfi = next(r for r in queries.delays(conn, "cause") if r["cause"] == "rfi")
    assert (rfi["hours"], rfi["rfis"]) == (4.5, "9, 14")


def test_delays_by_day(conn):
    rows = queries.delays(conn, "day", WEEK)
    assert [(r["report_date"], r["hours"]) for r in rows] == [
        ("2026-09-21", 5.0),
        ("2026-09-22", 9.0),
    ]


def test_week_must_start_on_monday():
    with pytest.raises(ValueError, match="Tuesday"):
        queries.week_range(date(2026, 9, 22))


def test_empty_week_returns_no_rows(conn):
    assert queries.manhours(conn, "sub", date(2026, 8, 31)) == []
