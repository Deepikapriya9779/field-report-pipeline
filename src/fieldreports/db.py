"""SQLite storage: one table per report section, plus the file ledger and the review queue.

``files`` records every file that reached a final state (loaded or review), keyed by the sha256
of its bytes. That one table is what makes ingest idempotent. Reports waiting for the LLM are
not recorded, so a later run picks them up.

Review items never touch the fact tables, so a query can't count a report a human hasn't
cleared.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from fieldreports.loaders import SourceDoc
from fieldreports.schema import DailyReport, DelayCause, InspectionResult, SafetyType


def _values(enum) -> str:
    return ", ".join(f"'{e.value}'" for e in enum)


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS files (
    sha256       TEXT PRIMARY KEY,
    path         TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('loaded', 'review')),
    ingested_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id                   INTEGER PRIMARY KEY,
    file_sha256          TEXT NOT NULL UNIQUE REFERENCES files (sha256),
    project              TEXT NOT NULL,
    report_date          TEXT NOT NULL,          -- ISO date, so string order is date order
    superintendent       TEXT NOT NULL,
    weather_conditions   TEXT NOT NULL,
    temp_high_f          INTEGER,
    temp_low_f           INTEGER,
    weather_delay_hours  REAL NOT NULL,
    notes                TEXT NOT NULL,
    source_file          TEXT NOT NULL,
    extraction_method    TEXT NOT NULL CHECK (extraction_method IN ('rules', 'llm')),
    confidence           REAL NOT NULL,
    UNIQUE (project, report_date)                -- one daily per job per day
);

CREATE TABLE IF NOT EXISTS manpower (
    report_id  INTEGER NOT NULL REFERENCES reports (id) ON DELETE CASCADE,
    company    TEXT NOT NULL,
    trade      TEXT NOT NULL,
    headcount  INTEGER NOT NULL CHECK (headcount >= 0),
    hours      REAL NOT NULL CHECK (hours >= 0)  -- per worker; man-hours = headcount * hours
);

CREATE TABLE IF NOT EXISTS work_items (
    report_id    INTEGER NOT NULL REFERENCES reports (id) ON DELETE CASCADE,
    area         TEXT NOT NULL,
    trade        TEXT NOT NULL,
    description  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delays (
    report_id    INTEGER NOT NULL REFERENCES reports (id) ON DELETE CASCADE,
    cause        TEXT NOT NULL CHECK (cause IN ({_values(DelayCause)})),
    hours        REAL NOT NULL CHECK (hours >= 0),  -- clock hours of stoppage, as reported
    description  TEXT NOT NULL,
    rfi_no       INTEGER
);

CREATE TABLE IF NOT EXISTS safety_events (
    report_id    INTEGER NOT NULL REFERENCES reports (id) ON DELETE CASCADE,
    type         TEXT NOT NULL CHECK (type IN ({_values(SafetyType)})),
    description  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inspections (
    report_id         INTEGER NOT NULL REFERENCES reports (id) ON DELETE CASCADE,
    type              TEXT NOT NULL,
    inspector_agency  TEXT NOT NULL,
    result            TEXT NOT NULL CHECK (result IN ({_values(InspectionResult)})),
    notes             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    report_id  INTEGER NOT NULL REFERENCES reports (id) ON DELETE CASCADE,
    supplier   TEXT NOT NULL,
    material   TEXT NOT NULL,
    notes      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visitors (
    report_id  INTEGER NOT NULL REFERENCES reports (id) ON DELETE CASCADE,
    name       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_queue (
    file_sha256   TEXT PRIMARY KEY REFERENCES files (sha256),
    path          TEXT NOT NULL,
    reasons       TEXT NOT NULL,   -- JSON list of strings
    payload_json  TEXT,            -- the extracted report, when there was one
    queued_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reports_date ON reports (report_date);
CREATE INDEX IF NOT EXISTS idx_manpower_report ON manpower (report_id);
CREATE INDEX IF NOT EXISTS idx_delays_report ON delays (report_id);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def is_known(conn: sqlite3.Connection, sha256: str) -> bool:
    return conn.execute("SELECT 1 FROM files WHERE sha256 = ?", (sha256,)).fetchone() is not None


def loaded_from(conn: sqlite3.Connection, project: str, report_date: str) -> str | None:
    """Source file of the report already loaded for this job and day, if any."""
    row = conn.execute(
        "SELECT source_file FROM reports WHERE project = ? AND report_date = ?",
        (project, report_date),
    ).fetchone()
    return row["source_file"] if row else None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _record_file(conn: sqlite3.Connection, doc: SourceDoc, status: str) -> None:
    conn.execute(
        "INSERT INTO files (sha256, path, status, ingested_at) VALUES (?, ?, ?, ?)",
        (doc.sha256, str(doc.path), status, _now()),
    )


def save_report(conn: sqlite3.Connection, doc: SourceDoc, r: DailyReport) -> int:
    """Insert one accepted report and all its rows in a single transaction."""
    with conn:
        _record_file(conn, doc, "loaded")
        cur = conn.execute(
            """INSERT INTO reports (file_sha256, project, report_date, superintendent,
                   weather_conditions, temp_high_f, temp_low_f, weather_delay_hours, notes,
                   source_file, extraction_method, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                doc.sha256, r.project, r.report_date.isoformat(), r.superintendent,
                r.weather.conditions, r.weather.temp_high_f, r.weather.temp_low_f,
                r.weather.weather_delay_hours, r.notes, r.source_file,
                r.extraction_method.value, r.confidence,
            ),
        )  # fmt: skip
        rid = cur.lastrowid
        conn.executemany(
            "INSERT INTO manpower VALUES (?, ?, ?, ?, ?)",
            [(rid, m.company, m.trade, m.headcount, m.hours) for m in r.manpower],
        )
        conn.executemany(
            "INSERT INTO work_items VALUES (?, ?, ?, ?)",
            [(rid, w.area, w.trade, w.description) for w in r.work_completed],
        )
        conn.executemany(
            "INSERT INTO delays VALUES (?, ?, ?, ?, ?)",
            [(rid, d.cause.value, d.hours, d.description, d.rfi_no) for d in r.delays],
        )
        conn.executemany(
            "INSERT INTO safety_events VALUES (?, ?, ?)",
            [(rid, s.type.value, s.description) for s in r.safety_events],
        )
        conn.executemany(
            "INSERT INTO inspections VALUES (?, ?, ?, ?, ?)",
            [(rid, i.type, i.inspector_agency, i.result.value, i.notes) for i in r.inspections],
        )
        conn.executemany(
            "INSERT INTO deliveries VALUES (?, ?, ?, ?)",
            [(rid, d.supplier, d.material, d.notes) for d in r.deliveries],
        )
        conn.executemany("INSERT INTO visitors VALUES (?, ?)", [(rid, v) for v in r.visitors])
    return rid


def save_review(
    conn: sqlite3.Connection, doc: SourceDoc, reasons: list[str], report: DailyReport | None
) -> None:
    payload = report.model_dump_json() if report else None
    with conn:
        _record_file(conn, doc, "review")
        conn.execute(
            "INSERT INTO review_queue VALUES (?, ?, ?, ?, ?)",
            (doc.sha256, str(doc.path), json.dumps(reasons), payload, _now()),
        )
