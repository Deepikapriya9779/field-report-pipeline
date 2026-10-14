from datetime import date

import pytest
from conftest import RAW

from fieldreports.loaders import discover, load, load_text
from fieldreports.rules import parse
from fieldreports.validate import REVIEW_THRESHOLD, text_problem, validate

TODAY = date(2026, 10, 14)
SOURCE = RAW / "DR_2026-09-22_HarborPoint.txt"


@pytest.fixture
def clean(roster):
    """A correct report and its source text: rules output for the 9/22 rain day."""
    text = load_text(SOURCE)
    report, unknown = roster.normalize(parse(text))
    assert unknown == []
    return report, text


def _check(report, text, unknown=(), **kw):
    return validate(report, text, list(unknown), today=TODAY, **kw)


def test_clean_report_is_accepted_with_full_confidence(clean):
    verdict = _check(*clean)
    assert verdict.accepted
    assert verdict.confidence == 1.0
    assert verdict.warnings == []


@pytest.mark.parametrize("field", ["project", "report_date", "superintendent"])
def test_missing_required_field_goes_to_review(clean, field):
    report, text = clean
    verdict = _check(report.model_copy(update={field: None}), text)
    assert not verdict.accepted
    assert f"missing required field: {field}" in verdict.reasons


def test_future_date_goes_to_review(clean):
    report, text = clean
    verdict = _check(report.model_copy(update={"report_date": date(2026, 10, 15)}), text)
    assert verdict.reasons == ["report date 2026-10-15 is in the future"]


def test_today_is_not_the_future(clean):
    report, text = clean
    assert _check(report.model_copy(update={"report_date": TODAY}), text).accepted


@pytest.mark.parametrize("headcount,ok", [(500, True), (501, False)])
def test_headcount_limit(clean, headcount, ok):
    report, text = clean
    report.manpower[0].headcount = headcount
    text += f"\n{headcount}\n"  # keep the number grounded so only the limit is tested
    verdict = _check(report, text)
    assert verdict.accepted is ok
    if not ok:
        assert "headcount 501" in verdict.reasons[0]


def test_more_than_24_hours_per_worker_goes_to_review(clean):
    report, text = clean
    report.manpower[1].hours = 25
    verdict = _check(report, text + "\n25\n")
    assert verdict.reasons == ["25 hours per worker for Keystone Interiors LLC exceeds 24"]


def test_delay_longer_than_a_day_goes_to_review(clean):
    report, text = clean
    report.delays[0].hours = 30
    report.weather.weather_delay_hours = 30
    verdict = _check(report, text + "\n30\n")
    assert len(verdict.reasons) == 2


def test_one_number_missing_from_the_source_costs_confidence_but_still_loads(clean):
    report, text = clean
    report.manpower[1].headcount = 77  # not in the text anywhere
    verdict = _check(report, text)
    assert verdict.accepted
    assert verdict.confidence == 0.85
    assert verdict.warnings == ["77 (Keystone Interiors LLC headcount) not in source text"]


def test_two_ungrounded_numbers_drop_below_the_threshold(clean):
    report, text = clean
    report.manpower[1].headcount = 77
    report.manpower[2].hours = 9.5
    verdict = _check(report, text)
    assert verdict.confidence == 0.70 < REVIEW_THRESHOLD
    assert not verdict.accepted
    assert verdict.reasons[0].startswith("low confidence 0.70: 77 (Keystone")


def test_retry_and_unknown_company_penalties_add_up(clean):
    report, text = clean
    verdict = _check(report, text, ["company 'Acme Painting'"], used_retry=True)
    assert verdict.confidence == 0.80
    assert verdict.accepted  # exactly at the threshold still loads


def test_report_with_no_crews_goes_to_review(clean):
    report, text = clean
    verdict = _check(report.model_copy(update={"manpower": []}), text)
    assert verdict.confidence == 0.70
    assert not verdict.accepted


def test_report_for_another_project_goes_to_review(clean):
    report, text = clean
    verdict = _check(report, text, ["project 'Eastgate Library'"])
    assert verdict.reasons == ["project 'Eastgate Library' is not this job"]


def test_weather_hours_that_disagree_with_the_delay_list_are_a_warning_not_a_rejection(clean):
    report, text = clean
    report.weather.weather_delay_hours = 6
    verdict = _check(report, text + "\n6\n")
    assert verdict.accepted
    assert "weather delay 6 hrs in weather section vs 8 hrs" in verdict.warnings[0]


def test_garbled_scan_is_caught_before_extraction():
    assert "garbled" in text_problem(load_text(RAW / "SCAN0047.txt"))


def test_empty_text_layer_is_caught():
    assert "no extractable text" in text_problem("\n\n  \n")


@pytest.mark.parametrize(
    "path", [p for p in discover(RAW) if p.name != "SCAN0047.txt"], ids=lambda p: p.name
)
def test_no_real_report_is_flagged_as_garbled(path):
    assert text_problem(load(path).text) is None


def test_template_safety_heading_alone_is_not_a_safety_line(clean):
    from fieldreports.validate import safety_lines

    report, text = clean
    assert "SAFETY" in text and safety_lines(text) == []
    assert _check(report, text).confidence == 1.0


def test_dropped_safety_event_sends_the_report_to_review(clean):
    """Recall check: confidence can't see a fact the extractor dropped, so count safety lines."""
    report, text = clean
    text += "\nsafety obs: ladder missing a foot found on l2; tagged out and removed\n"
    verdict = _check(report, text)
    assert verdict.confidence == 0.75
    assert (
        "1 safety-related line(s) in the text but 0 safety event(s) extracted"
        in (verdict.reasons[0])
    )


def test_extracted_safety_event_satisfies_the_recall_check(clean):
    report, text = clean
    text += "\nnear miss: grid bundle slid off lift deck, no injury\n"
    report.safety_events = [{"type": "near_miss", "description": "grid bundle slid off lift"}]
    assert _check(report, text).accepted
