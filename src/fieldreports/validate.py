"""Decide whether an extracted report is trustworthy enough to load.

Two kinds of problems:

* **Hard failures** always send the report to review: a missing date, an impossible number, a
  project that isn't ours. A wrong man-hour total in front of an owner costs more than a human
  spending a minute on the review queue.
* **Confidence** is a heuristic score, not a calibrated probability. It starts at 1.0 and loses
  points for signs the extraction may be wrong. The biggest signal is a number that does not
  appear anywhere in the source text: the extractor produced it from nothing. Below
  ``REVIEW_THRESHOLD`` the report goes to review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from fieldreports.schema import DelayCause, ReportFields

MAX_HEADCOUNT = 500  # bigger than any single sub's crew on a building this size, by a lot
MAX_HOURS = 24
REVIEW_THRESHOLD = 0.80
MIN_WORDLIKE_RATIO = 0.5  # clean reports score >= 0.89, the garbled OCR sample 0.12

PENALTY_UNGROUNDED_NUMBER = 0.15
PENALTY_RETRY = 0.10
PENALTY_UNKNOWN_COMPANY = 0.10
PENALTY_NO_MANPOWER = 0.30
PENALTY_UNEXTRACTED_SAFETY = 0.25  # one is enough for review: a missed recordable costs more

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_WORDLIKE = re.compile(r"^[(\[]?[A-Za-z]+(?:['/&.-][A-Za-z0-9]+)*[)\].,;:]*$")
# Words that put a line on the safety record. Deliberately broad: a false alarm costs a minute
# of review; a dropped near miss or recordable is a gap in the safety record.
_SAFETY_WORDS = re.compile(
    r"\b(safety|near[ _-]?miss|first[ _-]?aid|recordable|injur\w*|osha|tagged[ -]?out|"
    r"lock[ -]?out|incident)\b",
    re.IGNORECASE,
)


@dataclass
class Verdict:
    confidence: float
    reasons: list[str] = field(default_factory=list)  # why it goes to review
    warnings: list[str] = field(default_factory=list)  # loaded, but worth a look

    @property
    def accepted(self) -> bool:
        return not self.reasons


def text_problem(text: str) -> str | None:
    """Reject text no extractor should spend time (or tokens) on."""
    tokens = [t for t in text.split() if any(c.isalpha() for c in t)]
    if len(tokens) < 10:
        return "no extractable text (scanned PDF without a text layer?)"
    ratio = sum(bool(_WORDLIKE.match(t)) for t in tokens) / len(tokens)
    if ratio < MIN_WORDLIKE_RATIO:
        return f"text looks garbled ({ratio:.0%} of tokens are words; OCR noise?)"
    return None


def validate(
    report: ReportFields,
    source_text: str,
    unknown_names: list[str],
    *,
    today: date,
    used_retry: bool = False,
) -> Verdict:
    reasons = _hard_failures(report, unknown_names, today)

    penalties: list[tuple[float, str]] = []
    for number, where in _ungrounded_numbers(report, source_text):
        penalties.append((PENALTY_UNGROUNDED_NUMBER, f"{number:g} ({where}) not in source text"))
    if used_retry:
        penalties.append((PENALTY_RETRY, "extraction needed a retry"))
    for name in unknown_names:
        if name.startswith("company"):
            penalties.append((PENALTY_UNKNOWN_COMPANY, f"{name} not on roster"))
    if not report.manpower:
        penalties.append((PENALTY_NO_MANPOWER, "no manpower listed"))
    lines = safety_lines(source_text)
    missing = len(lines) - len(report.safety_events)
    if missing > 0:
        penalties.append(
            (
                PENALTY_UNEXTRACTED_SAFETY * missing,
                f"{len(lines)} safety-related line(s) in the text but "
                f"{len(report.safety_events)} safety event(s) extracted, e.g. {lines[0][:60]!r}",
            )
        )

    confidence = round(max(0.0, 1.0 - sum(p for p, _ in penalties)), 2)
    if confidence < REVIEW_THRESHOLD:
        details = "; ".join(why for _, why in penalties)
        reasons.append(f"low confidence {confidence:.2f}: {details}")

    warnings = [why for _, why in penalties] if confidence >= REVIEW_THRESHOLD else []
    weather_delays = sum(d.hours for d in report.delays if d.cause is DelayCause.WEATHER)
    if report.weather.weather_delay_hours != weather_delays:
        warnings.append(
            f"weather delay {report.weather.weather_delay_hours:g} hrs in weather section vs "
            f"{weather_delays:g} hrs of weather delays listed"
        )
    return Verdict(confidence=confidence, reasons=reasons, warnings=warnings)


def safety_lines(text: str) -> list[str]:
    """Lines that mention a safety event. A recall check: confidence (a precision check on
    numbers) can't see a fact the extractor dropped, so count what should be there."""
    return [
        line.strip()
        for line in text.splitlines()
        if _SAFETY_WORDS.search(line) and line.strip().upper() != "SAFETY"  # template heading
    ]


def _hard_failures(report: ReportFields, unknown_names: list[str], today: date) -> list[str]:
    reasons = [
        f"missing required field: {name}"
        for name in ("project", "report_date", "superintendent")
        if getattr(report, name) is None
    ]
    reasons += [f"{name} is not this job" for name in unknown_names if name.startswith("project")]
    if report.report_date and report.report_date > today:
        reasons.append(f"report date {report.report_date} is in the future")
    for crew in report.manpower:
        if crew.headcount > MAX_HEADCOUNT:
            reasons.append(f"headcount {crew.headcount} for {crew.company} exceeds {MAX_HEADCOUNT}")
        if crew.hours > MAX_HOURS:
            reasons.append(
                f"{crew.hours:g} hours per worker for {crew.company} exceeds {MAX_HOURS}"
            )
    for delay in report.delays:
        if delay.hours > MAX_HOURS:
            reasons.append(f"{delay.cause} delay of {delay.hours:g} hours exceeds {MAX_HOURS}")
    if report.weather.weather_delay_hours > MAX_HOURS:
        reasons.append(f"weather delay of {report.weather.weather_delay_hours:g} hours exceeds 24")
    return reasons


def _ungrounded_numbers(report: ReportFields, text: str) -> list[tuple[float, str]]:
    """Extracted numbers that never appear in the source text. Zeros are defaults, not claims."""
    in_text = {float(n) for n in _NUMBER.findall(text)}
    claims: list[tuple[float | None, str]] = [
        (report.weather.temp_high_f, "high temp"),
        (report.weather.temp_low_f, "low temp"),
        (report.weather.weather_delay_hours, "weather delay hours"),
    ]
    for crew in report.manpower:
        claims += [
            (crew.headcount, f"{crew.company} headcount"),
            (crew.hours, f"{crew.company} hours"),
        ]
    for delay in report.delays:
        claims += [(delay.hours, f"{delay.cause} delay hours"), (delay.rfi_no, "RFI number")]
    return [(float(n), where) for n, where in claims if n and float(n) not in in_text]
