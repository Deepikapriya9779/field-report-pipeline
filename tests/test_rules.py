from datetime import date

import pytest
from conftest import RAW, load_gold

from fieldreports.loaders import discover, load_text
from fieldreports.rules import RulesParseError, is_template, parse

TEMPLATE_FILES = [p for p in discover(RAW) if p.name.startswith("DR_2026")]
FREEFORM_FILES = [p for p in discover(RAW) if not p.name.startswith("DR_")]
TEMPLATE_GOLD = [(src, gold) for src, gold in load_gold() if src.startswith("DR_")]


def test_fixture_split_matches_the_generated_data():
    assert len(TEMPLATE_FILES) == 19  # 17 .txt + 2 .pdf
    assert len(TEMPLATE_GOLD) == 5


@pytest.mark.parametrize("path", TEMPLATE_FILES, ids=lambda p: p.name)
def test_every_templated_report_parses_with_the_date_from_its_filename(path):
    report = parse(load_text(path))
    assert report.report_date == date.fromisoformat(path.name[3:13])
    assert report.manpower, "every working day has a crew"


@pytest.mark.parametrize("source,gold", TEMPLATE_GOLD, ids=[s for s, _ in TEMPLATE_GOLD])
def test_rules_output_matches_gold_exactly(roster, source, gold):
    """Templates are the easy case: the rules parser should be perfect on them, PDF included."""
    parsed, unknown = roster.normalize(parse(load_text(RAW / source)))
    assert unknown == []
    assert parsed == gold


@pytest.mark.parametrize("path", FREEFORM_FILES, ids=lambda p: p.name)
def test_free_form_reports_are_not_mistaken_for_the_template(path):
    text = load_text(path)
    assert not is_template(text)
    with pytest.raises(RulesParseError, match="not a DR-1 template"):
        parse(text)


def test_blank_date_parses_as_none_instead_of_guessing():
    report = parse(load_text(RAW / "DR_HarborPoint_draft.txt"))
    assert report.report_date is None
    assert report.project == "Harbor Point Medical Office Building"


def _template_with(old: str, new: str) -> str:
    text = load_text(RAW / "DR_2026-09-22_HarborPoint.txt")
    assert old in text
    return text.replace(old, new)


def test_manpower_row_with_missing_column_raises():
    bad = _template_with("| hvac           | 6         | 8", "| hvac | 6")
    with pytest.raises(RulesParseError, match="columns"):
        parse(bad)


def test_non_iso_date_raises_so_the_llm_can_try():
    with pytest.raises(RulesParseError, match="YYYY-MM-DD"):
        parse(_template_with("2026-09-22", "9/22/26"))


def test_unknown_delay_cause_raises():
    with pytest.raises(RulesParseError, match="validation"):
        parse(_template_with("- weather | 8 hrs", "- lunch | 8 hrs"))


def test_wrapped_line_continues_the_previous_item():
    text = _template_with(
        "- [L2 west] (hvac) Insulated supply duct",
        "- [L2 west] (hvac) Insulated supply duct\nand return air plenum",
    )
    work = parse(text).work_completed
    assert work[0].description == "Insulated supply duct and return air plenum"


def test_rfi_number_is_parsed_from_its_own_column():
    delays = parse(load_text(RAW / "DR_2026-09-16_HarborPoint.txt")).delays
    assert [(d.cause.value, d.hours, d.rfi_no) for d in delays] == [("rfi", 3.0, 11)]
