"""Routing tests. The LLM is replaced by a small stub that returns fixed objects.

These stubs test the routing around the LLM. They are not stand-ins for model output, and
no eval number is computed from them.
"""

from collections import Counter
from datetime import date

from conftest import RAW, load_gold

from fieldreports.extract import LLMExtraction
from fieldreports.loaders import discover, load
from fieldreports.pipeline import Status, process
from fieldreports.schema import ExtractionMethod

TODAY = date(2026, 10, 14)


class StubLLM:
    def __init__(self, result: LLMExtraction):
        self.result = result
        self.calls = 0

    def extract(self, text: str) -> LLMExtraction:
        self.calls += 1
        return self.result


def test_template_report_loads_through_rules_with_full_confidence(roster):
    outcome = process(load(RAW / "DR_2026-09-22_HarborPoint.txt"), roster, today=TODAY)
    assert outcome.status is Status.LOADED
    assert outcome.report.extraction_method is ExtractionMethod.RULES
    assert outcome.report.confidence == 1.0
    assert outcome.report.source_file == "DR_2026-09-22_HarborPoint.txt"


def test_both_broken_reports_land_in_review_with_a_reason(roster):
    draft = process(load(RAW / "DR_HarborPoint_draft.txt"), roster, today=TODAY)
    scan = process(load(RAW / "SCAN0047.txt"), roster, today=TODAY)
    assert draft.status is scan.status is Status.REVIEW
    assert draft.reasons == ["missing required field: report_date"]
    assert "garbled" in scan.reasons[0]


def test_garbled_text_never_reaches_the_llm(roster):
    llm = StubLLM(LLMExtraction(report=None, attempts=1, error="unused"))
    process(load(RAW / "SCAN0047.txt"), roster, today=TODAY, llm=llm)
    assert llm.calls == 0


def test_template_reports_never_reach_the_llm(roster):
    llm = StubLLM(LLMExtraction(report=None, attempts=1, error="unused"))
    process(load(RAW / "DR_2026-09-16_HarborPoint.txt"), roster, today=TODAY, llm=llm)
    assert llm.calls == 0


def test_free_form_is_pending_until_an_llm_is_configured(roster):
    outcome = process(load(RAW / "daily 9-25.txt"), roster, today=TODAY)
    assert outcome.status is Status.PENDING
    assert "needs the LLM extractor" in outcome.reasons[0]


def test_llm_failure_goes_to_review_with_its_error(roster):
    llm = StubLLM(LLMExtraction(report=None, attempts=2, error="still invalid after retry: ..."))
    outcome = process(load(RAW / "daily 9-25.txt"), roster, today=TODAY, llm=llm)
    assert outcome.status is Status.REVIEW
    assert outcome.reasons == ["still invalid after retry: ..."]


def test_llm_report_is_normalized_validated_and_retry_is_penalized(roster):
    gold = dict(load_gold())["daily 9-25.txt"]
    as_written = gold.model_copy(deep=True)
    as_written.manpower[2].company = "electricians"  # what the text says; roster resolves it
    llm = StubLLM(LLMExtraction(report=as_written, attempts=2))
    outcome = process(load(RAW / "daily 9-25.txt"), roster, today=TODAY, llm=llm)
    assert outcome.status is Status.LOADED
    assert outcome.report.extraction_method is ExtractionMethod.LLM
    assert outcome.report.manpower[2].company == "Volt Line Electric, Inc."
    assert outcome.report.confidence == 0.90


def test_whole_folder_without_llm(roster):
    outcomes = [process(load(p), roster, today=TODAY) for p in discover(RAW)]
    assert Counter(o.status for o in outcomes) == {
        Status.LOADED: 19,  # every DR-1 template, .txt and .pdf
        Status.PENDING: 11,  # Marcus's free-form notes wait for the LLM milestone
        Status.REVIEW: 2,  # undated draft + garbled scan
    }
