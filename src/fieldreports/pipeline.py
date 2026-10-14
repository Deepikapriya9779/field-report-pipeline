"""One document in, one outcome out: loaded, sent to review, or pending (needs the LLM)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from fieldreports.extract import LLMExtractor, extract
from fieldreports.loaders import SourceDoc
from fieldreports.roster import Roster
from fieldreports.schema import DailyReport
from fieldreports.validate import text_problem, validate


class Status(StrEnum):
    LOADED = "loaded"
    REVIEW = "review"
    PENDING = "pending"


@dataclass
class Outcome:
    doc: SourceDoc
    status: Status
    report: DailyReport | None = None
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def process(
    doc: SourceDoc, roster: Roster, *, today: date, llm: LLMExtractor | None = None
) -> Outcome:
    if problem := text_problem(doc.text):
        return Outcome(doc, Status.REVIEW, reasons=[problem])

    extraction = extract(doc.text, llm)
    if extraction.pending:
        return Outcome(doc, Status.PENDING, reasons=[extraction.error or "needs the LLM"])
    if extraction.report is None:
        return Outcome(doc, Status.REVIEW, reasons=[extraction.error or "extraction failed"])

    fields, unknown = roster.normalize(extraction.report)
    verdict = validate(fields, doc.text, unknown, today=today, used_retry=extraction.used_retry)
    report = DailyReport(
        **fields.model_dump(),
        source_file=doc.path.name,
        extraction_method=extraction.method,
        confidence=verdict.confidence,
    )
    status = Status.LOADED if verdict.accepted else Status.REVIEW
    return Outcome(doc, status, report, verdict.reasons, verdict.warnings)
