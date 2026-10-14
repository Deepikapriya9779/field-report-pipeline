from datetime import date

import pytest
from pydantic import ValidationError

from fieldreports.schema import (
    PIPELINE_FIELDS,
    DailyReport,
    DelayCause,
    ManpowerEntry,
    ReportFields,
    Weather,
)


def _minimal(**overrides):
    data = {
        "project": "Harbor Point Medical Office Building",
        "report_date": "2026-09-21",
        "superintendent": "Dana Okafor",
        "weather": {"conditions": "Clear", "temp_high_f": 78, "temp_low_f": 61},
    }
    data.update(overrides)
    return data


def test_minimal_report_parses_and_defaults_lists():
    report = DailyReport.model_validate(_minimal())
    assert report.report_date == date(2026, 9, 21)
    assert report.manpower == []
    assert report.weather.weather_delay_hours == 0


def test_header_fields_are_nullable_so_extractors_never_have_to_guess():
    report = ReportFields.model_validate(_minimal(report_date=None, superintendent=None))
    assert report.report_date is None
    assert report.superintendent is None


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        DailyReport.model_validate(_minimal(foreman="someone"))


def test_delay_cause_must_be_in_the_enum():
    with pytest.raises(ValidationError):
        DailyReport.model_validate(
            _minimal(delays=[{"cause": "lunch", "hours": 1, "description": "x"}])
        )
    ok = DailyReport.model_validate(
        _minimal(delays=[{"cause": "rfi", "hours": 2, "description": "x", "rfi_no": 14}])
    )
    assert ok.delays[0].cause is DelayCause.RFI


@pytest.mark.parametrize("field,value", [("headcount", -1), ("hours", -0.5)])
def test_negative_manpower_numbers_are_a_shape_error(field, value):
    data = {"company": "Keystone Interiors LLC", "trade": "drywall", "headcount": 6, "hours": 8}
    data[field] = value
    with pytest.raises(ValidationError):
        ManpowerEntry.model_validate(data)


def test_total_manhours_is_headcount_times_hours_per_worker():
    report = DailyReport.model_validate(
        _minimal(
            manpower=[
                {"company": "A", "trade": "drywall", "headcount": 8, "hours": 8},
                {"company": "B", "trade": "electrical", "headcount": 6, "hours": 10},
            ]
        )
    )
    assert report.total_manhours == 8 * 8 + 6 * 10


def test_llm_facing_schema_excludes_pipeline_owned_fields():
    llm_props = set(ReportFields.model_json_schema()["properties"])
    assert PIPELINE_FIELDS == {"source_file", "extraction_method", "confidence"}
    assert not (llm_props & PIPELINE_FIELDS)


def test_weather_delay_hours_cannot_be_negative():
    with pytest.raises(ValidationError):
        Weather(conditions="Rain", weather_delay_hours=-1)
