"""The data model for one superintendent daily report.

Two layers:

* ``ReportFields`` is what an extractor (rules or LLM) is responsible for. Its JSON schema is what
  the LLM sees as the tool's ``input_schema``.
* ``DailyReport`` adds the fields the pipeline owns (where the report came from, how it was
  extracted, how much we trust it). The LLM never sets these.

The header fields (project, date, superintendent) are nullable on purpose: an extractor must be
able to say "the report doesn't state this" instead of guessing. Whether a missing value is
acceptable is decided later, in ``validate.py``, which sends the report to the review queue.

Pydantic checks *shape* (types, enums, non-negative numbers). Domain plausibility checks
(headcount > 500, more than 24 hours per worker, dates in the future) live in ``validate.py`` so
that they become review reasons rather than something an LLM retry could "fix" by changing a
number until it passes.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class DelayCause(StrEnum):
    WEATHER = "weather"
    RFI = "rfi"
    MATERIAL = "material"
    INSPECTION = "inspection"
    OWNER_CHANGE = "owner_change"
    LABOR = "labor"
    EQUIPMENT = "equipment"
    OTHER = "other"


class SafetyType(StrEnum):
    NEAR_MISS = "near_miss"
    FIRST_AID = "first_aid"
    RECORDABLE = "recordable"
    OBSERVATION = "observation"


class InspectionResult(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    PARTIAL = "partial"


class ExtractionMethod(StrEnum):
    RULES = "rules"
    LLM = "llm"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Weather(_Model):
    conditions: str = Field(description="Short description, e.g. 'Heavy rain' or 'Clear'.")
    temp_high_f: int | None = Field(default=None, description="Daily high, degrees F.")
    temp_low_f: int | None = Field(default=None, description="Daily low, degrees F.")
    weather_delay_hours: float = Field(
        default=0, ge=0, description="Hours of work lost to weather, as reported. 0 if none."
    )


class ManpowerEntry(_Model):
    company: str = Field(description="Subcontractor or self-perform crew name.")
    trade: str = Field(description="Trade, e.g. drywall, electrical, hvac.")
    headcount: int = Field(ge=0, description="Workers on site from this company.")
    hours: float = Field(ge=0, description="Hours worked PER WORKER (shift length), not total.")


class WorkItem(_Model):
    area: str = Field(description="Location, e.g. 'L2 east corridor' or 'Roof'.")
    trade: str
    description: str


class Delay(_Model):
    cause: DelayCause
    hours: float = Field(ge=0, description="Clock hours of stoppage as reported (not crew-hours).")
    description: str
    rfi_no: int | None = Field(default=None, description="RFI number when cause is rfi.")


class SafetyEvent(_Model):
    type: SafetyType
    description: str


class Inspection(_Model):
    type: str = Field(description="What was inspected, e.g. 'Above-ceiling L1'.")
    inspector_agency: str = Field(description="Who inspected, e.g. city building dept.")
    result: InspectionResult
    notes: str = ""


class Delivery(_Model):
    supplier: str
    material: str
    notes: str = ""


class ReportFields(_Model):
    """Everything an extractor reads out of the report text."""

    project: str | None = Field(description="Project name as written. Null if not stated.")
    report_date: date | None = Field(
        description="Date the work happened (YYYY-MM-DD). Null if the report doesn't state it."
    )
    superintendent: str | None = Field(description="Who wrote the report. Null if not stated.")
    weather: Weather
    manpower: list[ManpowerEntry] = Field(default_factory=list)
    work_completed: list[WorkItem] = Field(default_factory=list)
    delays: list[Delay] = Field(default_factory=list)
    safety_events: list[SafetyEvent] = Field(default_factory=list)
    inspections: list[Inspection] = Field(default_factory=list)
    deliveries: list[Delivery] = Field(default_factory=list)
    visitors: list[str] = Field(default_factory=list)
    notes: str = ""


class DailyReport(ReportFields):
    """A report plus the provenance fields the pipeline sets."""

    source_file: str = ""
    extraction_method: ExtractionMethod | None = None
    confidence: float = Field(default=0.0, ge=0, le=1)

    @property
    def total_manhours(self) -> float:
        return sum(m.headcount * m.hours for m in self.manpower)


PIPELINE_FIELDS = frozenset(DailyReport.model_fields) - frozenset(ReportFields.model_fields)
