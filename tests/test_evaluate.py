import pytest
from conftest import load_gold

from fieldreports import llm
from fieldreports.evaluate import (
    SECTIONS,
    Counts,
    dice,
    evaluate,
    render_results,
    same,
    score_report,
    score_section,
    total,
)

CREW_A = {"company": "Keystone Interiors LLC", "trade": "drywall", "headcount": 8, "hours": 8}
CREW_B = {"company": "Volt Line Electric, Inc.", "trade": "electrical", "headcount": 6, "hours": 8}


def test_precision_recall_f1_arithmetic():
    c = Counts(tp=8, fp=2, fn=0)
    assert (c.precision, c.recall) == (0.8, 1.0)
    assert c.f1 == pytest.approx(2 * 0.8 / 1.8)
    assert Counts().f1 == 0.0


def test_text_match_uses_token_overlap():
    assert dice("L1 west", "L1 west corridor walls") == pytest.approx(2 * 2 / 6)
    assert same("text", "L1 west", "l1 WEST corridor walls")  # 0.67 >= 0.6
    assert not same("text", "L1 west", "L3 east")
    assert same("exact", 8, 8.0)
    assert not same("exact", 8, 9)


def test_list_order_does_not_matter():
    counts, errors = score_section("manpower", [CREW_A, CREW_B], [CREW_B, CREW_A])
    assert (counts.tp, counts.fp, counts.fn) == (8, 0, 0)
    assert errors == []


def test_a_wrong_value_is_one_false_positive_and_one_false_negative():
    wrong = {**CREW_A, "headcount": 9}
    counts, errors = score_section("manpower", [CREW_A, CREW_B], [wrong, CREW_B])
    assert (counts.tp, counts.fp, counts.fn) == (7, 1, 1)
    assert errors == ["manpower.headcount: 9, expected 8"]


def test_missed_and_extra_items_count_each_filled_field():
    counts, _ = score_section("manpower", [CREW_A, CREW_B], [CREW_A])
    assert (counts.tp, counts.fp, counts.fn) == (4, 0, 4)
    counts, _ = score_section("manpower", [CREW_A], [CREW_A, CREW_B])
    assert (counts.tp, counts.fp, counts.fn) == (4, 4, 0)


def test_empty_on_both_sides_is_not_a_fact():
    counts, errors = score_section("notes", [{"notes": ""}], [{"notes": ""}])
    assert (counts.tp, counts.fp, counts.fn) == (0, 0, 0)
    assert errors == []
    counts, _ = score_section(
        "deliveries",
        [{"supplier": "Coastal Gypsum Supply", "material": "board", "notes": ""}],
        [{"supplier": "Coastal Gypsum Supply", "material": "board", "notes": "8 bunks"}],
    )
    assert (counts.tp, counts.fp, counts.fn) == (2, 1, 0)


def test_gold_scored_against_itself_is_perfect():
    for source, gold in load_gold():
        score = score_report(source, gold, gold)
        assert score.errors == []
        assert total([score]).f1 == 1.0


def test_no_prediction_scores_zero_recall():
    source, gold = load_gold()[0]
    score = score_report(source, gold, None)
    c = total([score])
    assert c.tp == c.fp == 0 and c.fn > 0


def test_section_totals_add_up_to_the_overall_total():
    source, gold = load_gold()[0]
    score = score_report(source, gold, None)
    assert sum(total([score], s).fn for s in SECTIONS) == total([score]).fn


def test_eval_on_recorded_fixtures_is_deterministic(roster):
    """All three methods replay recorded responses (prompt extract-v2). Known results: rules
    and the LLM are both exact on the template reports; the LLM missed the 9/11 first aid."""
    extractor = llm.build_extractor(roster, llm.LLMMode.MOCK)
    run = evaluate(extractor, roster)
    templates = {m: [s for s in run.scores[m] if s.source in run.template] for m in run.scores}
    assert len(templates["rules"]) == 5
    assert total(templates["rules"]).f1 == 1.0
    assert total(templates["llm"]).f1 == 1.0

    errors = {s.source: s.errors for s in run.scores["llm"]}
    assert any("safety_events: missed item" in e for e in errors["daily 9-11.txt"])
    assert not any("safety_events" in e for e in errors["daily 9-25.txt"])
    assert total(run.scores["pipeline"]).f1 > 0.95
    assert len(run.usage) == 12  # one LLM call per gold report, not double-counted

    run.mode = "mock"
    markdown = render_results(run)
    assert "| Rules | template (5) | 1.000 | 1.000 | 1.000 |" in markdown
    assert "| visitors | n/a | n/a |" in markdown
