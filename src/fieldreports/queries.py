"""Every question the CLI and the weekly report ask, as plain SQL.

Conventions:
* Man-hours are always ``headcount * hours`` (hours are per worker), computed here, never stored.
* Delay hours are clock hours of stoppage as the super reported them. Two delays on the same day
  can overlap, so a day's total can exceed the hours actually lost. The weekly report says so.
* Date filters use ``:start``/``:end`` (inclusive ISO dates). "All time" is a wide range rather
  than a second copy of each query.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

ALL_TIME = ("0001-01-01", "9999-12-31")

MANHOURS_BY_SUB = """
SELECT m.company,
       m.trade,
       COUNT(DISTINCT r.id)        AS days_on_site,
       SUM(m.headcount)            AS worker_days,
       SUM(m.headcount * m.hours)  AS man_hours
FROM manpower m
JOIN reports r ON r.id = m.report_id
WHERE r.report_date BETWEEN :start AND :end
GROUP BY m.company, m.trade
ORDER BY man_hours DESC, m.company
"""

MANHOURS_BY_TRADE = """
SELECT m.trade,
       COUNT(DISTINCT m.company)   AS companies,
       SUM(m.headcount)            AS worker_days,
       SUM(m.headcount * m.hours)  AS man_hours
FROM manpower m
JOIN reports r ON r.id = m.report_id
WHERE r.report_date BETWEEN :start AND :end
GROUP BY m.trade
ORDER BY man_hours DESC, m.trade
"""

MANHOURS_BY_DAY = """
SELECT r.report_date,
       SUM(m.headcount)            AS headcount,
       SUM(m.headcount * m.hours)  AS man_hours
FROM reports r
JOIN manpower m ON m.report_id = r.id
WHERE r.report_date BETWEEN :start AND :end
GROUP BY r.report_date
ORDER BY r.report_date
"""

DELAYS_BY_CAUSE = """
SELECT d.cause,
       COUNT(*)                    AS events,
       SUM(d.hours)                AS hours,
       GROUP_CONCAT(DISTINCT d.rfi_no) AS rfis
FROM delays d
JOIN reports r ON r.id = d.report_id
WHERE r.report_date BETWEEN :start AND :end
GROUP BY d.cause
ORDER BY hours DESC, d.cause
"""

DELAYS_BY_DAY = """
SELECT r.report_date,
       COUNT(*)                    AS events,
       SUM(d.hours)                AS hours,
       GROUP_CONCAT(d.cause, ', ') AS causes
FROM delays d
JOIN reports r ON r.id = d.report_id
WHERE r.report_date BETWEEN :start AND :end
GROUP BY r.report_date
ORDER BY r.report_date
"""

REVIEW_QUEUE = """
SELECT path, reasons, queued_at
FROM review_queue
ORDER BY queued_at, path
"""


def week_range(week_start: date) -> tuple[str, str]:
    """Monday through Sunday, so a Saturday make-up day counts in its own week."""
    if week_start.weekday() != 0:
        raise ValueError(f"--week must be a Monday; {week_start} is a {week_start:%A}")
    return week_start.isoformat(), (week_start + timedelta(days=6)).isoformat()


def _run(conn: sqlite3.Connection, sql: str, week: date | None) -> list[dict]:
    start, end = week_range(week) if week else ALL_TIME
    return [dict(row) for row in conn.execute(sql, {"start": start, "end": end})]


def manhours(conn: sqlite3.Connection, by: str, week: date | None = None) -> list[dict]:
    sql = {"sub": MANHOURS_BY_SUB, "trade": MANHOURS_BY_TRADE, "day": MANHOURS_BY_DAY}[by]
    return _run(conn, sql, week)


def delays(conn: sqlite3.Connection, by: str, week: date | None = None) -> list[dict]:
    rows = _run(conn, {"cause": DELAYS_BY_CAUSE, "day": DELAYS_BY_DAY}[by], week)
    for row in rows:
        if "rfis" in row:  # GROUP_CONCAT order is unspecified; make it stable
            row["rfis"] = ", ".join(sorted(row["rfis"].split(","), key=int)) if row["rfis"] else ""
    return rows


def review_queue(conn: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in conn.execute(REVIEW_QUEUE)]
