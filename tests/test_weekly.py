"""Weekly report tests on a small hand-built week, so the numbers can be checked by hand.

Week of Mon 9/21 (Tue 9/22 and Wed 9/23 have no report):
  Mon 9/21  Keystone 8 x 8 = 64, Volt Line 6 x 10 = 60       rfi 2 hrs (RFI 14)
  Thu 9/24  Keystone 10 x 8 = 80                              weather 3 hrs; framing L2 FAILED
  Fri 9/25  Keystone 9 x 8 = 72                               near miss
Totals: 276 man-hours (Keystone 216, Volt Line 60), 5 delay hours.
"""

from datetime import date

import pytest

from fieldreports import db, llm
from fieldreports.llm import Narrative
from fieldreports.loaders import SourceDoc
from fieldreports.schema import DailyReport, ExtractionMethod
from fieldreports.weekly import build_weekly, gather, narrative_problem, template_narrative

WEEK = date(2026, 9, 21)
PROJECT = "Harbor Point Medical Office Building"
KEYSTONE = {"company": "Keystone Interiors LLC", "trade": "drywall"}
VOLT = {"company": "Volt Line Electric, Inc.", "trade": "electrical"}


def _report(day, crews, **sections):
    return DailyReport(
        project=PROJECT,
        report_date=day,
        superintendent="Dana Okafor",
        weather={"conditions": "Clear", "temp_high_f": 80, "temp_low_f": 65,
                 "weather_delay_hours": sections.pop("weather_hours", 0)},
        manpower=[{**sub, "headcount": n, "hours": h} for sub, n, h in crews],
        source_file=f"{day}.txt",
        extraction_method=ExtractionMethod.RULES,
        confidence=1.0,
        **sections,
    )  # fmt: skip


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "w.db")
    reports = [
        _report(
            date(2026, 9, 21),
            [(KEYSTONE, 8, 8), (VOLT, 6, 10)],
            delays=[{"cause": "rfi", "hours": 2, "rfi_no": 14, "description": "RFI 14"}],
        ),
        _report(
            date(2026, 9, 24),
            [(KEYSTONE, 10, 8)],
            weather_hours=3,
            delays=[{"cause": "weather", "hours": 3, "description": "rain"}],
            inspections=[{"type": "Framing L2", "inspector_agency": "City", "result": "fail"}],
        ),
        _report(
            date(2026, 9, 25),
            [(KEYSTONE, 9, 8)],
            safety_events=[{"type": "near_miss", "description": "dropped block"}],
        ),
    ]
    for r in reports:
        db.save_report(conn, SourceDoc(path=r.source_file, sha256=r.source_file, text=""), r)
    return conn


class StubNarrator:
    """Returns a fixed paragraph; tests the checks around the LLM, not the LLM."""

    model = "stub-model"

    def __init__(self, text=None, error=None):
        self.result = Narrative(text, error)
        self.calls = []

    def write(self, facts):
        self.calls.append(facts)
        return self.result


def test_facts_are_computed_in_sql(conn):
    f = gather(conn, WEEK, PROJECT)
    assert f["reports"] == 3
    assert f["total_man_hours"] == 276
    assert [(s["company"], s["man_hours"]) for s in f["man_hours_by_sub"]] == [
        ("Keystone Interiors LLC", 216.0),
        ("Volt Line Electric, Inc.", 60.0),
    ]
    assert f["total_delay_hours"] == 5
    assert f["weather_days"] == ["2026-09-24"]
    assert f["weekdays_without_report"] == ["2026-09-22", "2026-09-23"]


def test_template_narrative_states_the_key_numbers(conn):
    text = template_narrative(gather(conn, WEEK, PROJECT))
    assert "276 man-hours" in text
    assert "led by Keystone Interiors LLC with 216" in text
    assert "5 hours (RFI 2, weather 3)" in text or "5 hours (weather 3, RFI 2)" in text
    assert "1 of 1 inspections did not pass: Framing L2 (fail)" in text
    assert "near miss" in text
    assert "2 weekday(s)" in text


def test_narrative_with_an_invented_number_is_rejected(conn):
    facts = gather(conn, WEEK, PROJECT)
    assert narrative_problem("Crews logged 276 man-hours; RFI 14 cost 2 hours.", facts) is None
    assert narrative_problem("Crews logged 1,276 man-hours.", facts) == (
        "number 1,276 is not in the week's facts"
    )
    assert narrative_problem("First paragraph.\n\nSecond paragraph.", facts)


def test_llm_paragraph_is_used_when_it_checks_out(conn):
    narrator = StubNarrator("RFI 14 held work for 2 hours and Framing L2 failed.")
    markdown, source = build_weekly(conn, WEEK, PROJECT, narrator)
    assert source == "llm"
    assert "RFI 14 held work for 2 hours" in markdown
    assert "Written by stub-model" in markdown
    assert "raw" not in str(narrator.calls[0]).lower()  # facts only, never report text


def test_llm_paragraph_with_bad_numbers_falls_back_to_the_template(conn):
    markdown, source = build_weekly(conn, WEEK, PROJECT, StubNarrator("We lost 40 hours."))
    assert source == "template"
    assert "LLM draft was rejected (number 40 is not in the week's facts)" in markdown
    assert "276 man-hours" in markdown


def test_refusal_falls_back_to_the_template(conn):
    narrator = StubNarrator(error="model stopped with refusal")
    _, source = build_weekly(conn, WEEK, PROJECT, narrator)
    assert source == "template"


def test_markdown_has_the_tables_and_flags(conn):
    markdown, _ = build_weekly(conn, WEEK, PROJECT)
    assert "| **Total** |  |  |  | **276** |" in markdown
    assert "| Thu Sep 24 | Framing L2 | City | FAIL |" in markdown
    assert "**No daily report on file for:** Tue Sep 22, Wed Sep 23." in markdown
    assert "not crew-hours" in markdown


def test_empty_week_skips_the_llm(conn):
    narrator = StubNarrator("unused")
    markdown, source = build_weekly(conn, date(2026, 8, 31), PROJECT, narrator)
    assert source == "template" and narrator.calls == []
    assert "No daily reports were loaded for this week." in markdown


def test_mock_narrator_without_a_recording_fails_loudly(conn, tmp_path):
    narrator = llm.ClaudeNarrator(llm.ReplayTransport(llm.FixtureStore(tmp_path)))
    with pytest.raises(llm.FixtureError, match="never invents"):
        build_weekly(conn, WEEK, PROJECT, narrator)
