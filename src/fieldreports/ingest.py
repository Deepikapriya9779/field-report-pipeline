"""Ingest a folder: hash, skip what's known, extract, validate, then load or queue for review."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from fieldreports import db
from fieldreports.extract import LLMExtractor
from fieldreports.loaders import discover, file_sha256, load
from fieldreports.pipeline import Outcome, Status, process
from fieldreports.roster import Roster


@dataclass
class IngestResult:
    skipped: int = 0
    outcomes: list[Outcome] = field(default_factory=list)

    def count(self, status: Status) -> int:
        return sum(o.status is status for o in self.outcomes)


def ingest_folder(
    conn: sqlite3.Connection,
    folder: Path,
    roster: Roster,
    *,
    today: date,
    llm: LLMExtractor | None = None,
) -> IngestResult:
    result = IngestResult()
    for path in discover(folder):
        if db.is_known(conn, file_sha256(path)):
            result.skipped += 1
            continue
        outcome = process(load(path), roster, today=today, llm=llm)

        if outcome.status is Status.LOADED:
            r = outcome.report
            existing = db.loaded_from(conn, r.project, r.report_date.isoformat())
            if existing:
                outcome.status = Status.REVIEW
                outcome.reasons = [
                    f"a report for {r.report_date} is already loaded ({existing}); "
                    "revised report? a human picks which one counts"
                ]

        if outcome.status is Status.LOADED:
            db.save_report(conn, outcome.doc, outcome.report)
        elif outcome.status is Status.REVIEW:
            db.save_review(conn, outcome.doc, outcome.reasons, outcome.report)
        result.outcomes.append(outcome)  # pending: nothing saved, picked up on a later run
    return result
