"""Route a report to an extractor: rules first, the LLM only when rules can't read it.

The route depends on the text, not the file type: a PDF printed from the DR-1 form goes through
the rules parser like any other template.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fieldreports.rules import RulesParseError, is_template, parse
from fieldreports.schema import ExtractionMethod, ReportFields


@dataclass(frozen=True)
class LLMExtraction:
    report: ReportFields | None
    attempts: int
    error: str | None = None  # why there's no report (refusal, still invalid after the retry)


class LLMExtractor(Protocol):
    def extract(self, text: str) -> LLMExtraction: ...


@dataclass(frozen=True)
class Extraction:
    report: ReportFields | None
    method: ExtractionMethod | None
    used_retry: bool = False
    error: str | None = None
    pending: bool = False  # needs the LLM, and no LLM extractor is configured


def extract(text: str, llm: LLMExtractor | None = None) -> Extraction:
    rules_error = None
    if is_template(text):
        try:
            return Extraction(parse(text), ExtractionMethod.RULES)
        except RulesParseError as exc:
            rules_error = f"rules parser: {exc}"

    if llm is None:
        why = rules_error or "not the DR-1 template"
        return Extraction(None, None, error=f"{why}; needs the LLM extractor", pending=True)

    result = llm.extract(text)
    return Extraction(
        report=result.report,
        method=ExtractionMethod.LLM,
        used_retry=result.attempts > 1,
        error=result.error,
    )
