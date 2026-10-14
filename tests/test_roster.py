import pytest

from fieldreports.roster import Roster, name_key
from fieldreports.schema import ReportFields


@pytest.fixture(scope="module")
def roster():
    return Roster.load()


def test_name_key_drops_suffixes_and_punctuation():
    assert name_key("Volt Line Electric, Inc.") == "volt line electric"
    assert name_key("Gravel & Grade") == "gravel and grade"


@pytest.mark.parametrize(
    "written,company",
    [
        ("Volt Line Electric, Inc.", "Volt Line Electric, Inc."),
        ("volt line", "Volt Line Electric, Inc."),
        ("elec", "Volt Line Electric, Inc."),  # bare trade word, one sub has that trade
        ("framers", "Keystone Interiors LLC"),
        ("g&g site", "Gravel & Grade Sitework"),
        ("summit roofers", "Summit Roofing & Sheet Metal"),
    ],
)
def test_aliases_resolve_to_one_company(roster, written, company):
    assert roster.match_company(written).company == company


def test_unknown_company_is_not_guessed(roster):
    assert roster.match_company("Acme Painting") is None


def test_normalize_canonicalizes_and_reports_unknowns(roster):
    report = ReportFields.model_validate(
        {
            "project": "HP MOB",
            "report_date": "2026-09-25",
            "superintendent": "Marcus",
            "weather": {"conditions": "Sunny"},
            "manpower": [
                {"company": "elec", "trade": "elec", "headcount": 6, "hours": 8},
                {"company": "Acme Painting", "trade": "painters", "headcount": 2, "hours": 8},
            ],
            "work_completed": [{"area": "L2 east", "trade": "mech", "description": "duct"}],
        }
    )
    out, unknown = roster.normalize(report)
    assert out.project == "Harbor Point Medical Office Building"
    assert out.superintendent == "Marcus Reyes"
    assert (out.manpower[0].company, out.manpower[0].trade) == (
        "Volt Line Electric, Inc.",
        "electrical",
    )
    assert out.manpower[1].company == "Acme Painting"  # kept as written
    assert out.work_completed[0].trade == "hvac"
    assert unknown == ["company 'Acme Painting'"]
    assert report.manpower[0].company == "elec"  # input untouched
