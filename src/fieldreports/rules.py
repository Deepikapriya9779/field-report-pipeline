"""Deterministic parser for the company daily report form (Bayline Builders DR-1).

Rules go first because they're free, instant and exact. If the text isn't the template, or the
template was filled in a way the parser can't read, it raises ``RulesParseError`` and the report
goes to the LLM extractor instead. The parser never guesses: a blank date stays ``None`` and
validation decides what that means.
"""

from __future__ import annotations

import re
from datetime import date

from pydantic import ValidationError

from fieldreports.schema import ReportFields

TEMPLATE_MARKERS = ("SUPERINTENDENT DAILY REPORT", "Form DR-1")
SECTIONS = (
    "WEATHER",
    "MANPOWER",
    "WORK COMPLETED",
    "DELAYS",
    "SAFETY",
    "INSPECTIONS",
    "DELIVERIES",
    "VISITORS",
    "NOTES",
)
_WORK_ITEM = re.compile(r"^\[(?P<area>[^\]]+)\]\s*\((?P<trade>[^)]+)\)\s*(?P<description>.+)$")
_HOURS = re.compile(r"^(?P<n>\d+(?:\.\d+)?)\s*hrs?$", re.IGNORECASE)
_RFI = re.compile(r"^RFI\s*#?\s*(?P<n>\d+)$", re.IGNORECASE)


class RulesParseError(ValueError):
    """The text is not a DR-1 form, or a section doesn't follow the form's layout."""


def is_template(text: str) -> bool:
    return all(marker in text for marker in TEMPLATE_MARKERS)


def parse(text: str) -> ReportFields:
    if not is_template(text):
        raise RulesParseError("not a DR-1 template")
    header, sections = _split_sections(text)
    weather = _key_values(sections.get("WEATHER", []))
    high, low = _high_low(weather.get("high / low (f)", ""))

    data = {
        "project": header.get("project") or None,
        "report_date": _date(header.get("report date", "")),
        "superintendent": header.get("superintendent") or None,
        "weather": {
            "conditions": weather.get("conditions", ""),
            "temp_high_f": high,
            "temp_low_f": low,
            "weather_delay_hours": _number(weather.get("weather delay (hrs)") or "0", "delay"),
        },
        "manpower": _manpower(sections.get("MANPOWER", [])),
        "work_completed": [_work_item(i) for i in _items(sections.get("WORK COMPLETED", []))],
        "delays": [_delay(i) for i in _items(sections.get("DELAYS", []))],
        "safety_events": [
            dict(zip(("type", "description"), _cells(i, 2, 2), strict=True))
            for i in _items(sections.get("SAFETY", []))
        ],
        "inspections": [
            dict(
                zip(("type", "inspector_agency", "result", "notes"), _cells(i, 3, 4), strict=False)
            )
            for i in _items(sections.get("INSPECTIONS", []))
        ],
        "deliveries": [
            dict(zip(("supplier", "material", "notes"), _cells(i, 2, 3), strict=False))
            for i in _items(sections.get("DELIVERIES", []))
        ],
        "visitors": _items(sections.get("VISITORS", [])),
        "notes": _notes(sections.get("NOTES", [])),
    }
    try:
        return ReportFields.model_validate(data)
    except ValidationError as exc:
        raise RulesParseError(f"template values failed validation: {exc}") from exc


# -- layout ------------------------------------------------------------------------------------


def _split_sections(text: str) -> tuple[dict[str, str], dict[str, list[str]]]:
    header_lines: list[str] = []
    sections: dict[str, list[str]] = {}
    current: list[str] = header_lines
    for raw in text.splitlines():
        line = raw.strip()
        if line in SECTIONS:
            current = sections.setdefault(line, [])
        elif line:
            current.append(line)
    return _key_values(header_lines), sections


def _key_values(lines: list[str]) -> dict[str, str]:
    out = {}
    for line in lines:
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip().lower()] = value.strip()
    return out


def _items(lines: list[str]) -> list[str]:
    """'- ' starts an item; any other line continues the previous one (wrapped PDF lines)."""
    items: list[str] = []
    for line in lines:
        if line.startswith("- "):
            items.append(line[2:].strip())
        elif items:
            items[-1] = f"{items[-1]} {line}"
        else:
            raise RulesParseError(f"text before the first item: {line!r}")
    return [] if items == ["None"] else items


def _cells(item: str, min_cells: int, max_cells: int) -> list[str]:
    cells = [c.strip() for c in item.split("|")]
    if not min_cells <= len(cells) <= max_cells:
        raise RulesParseError(f"expected {min_cells}-{max_cells} columns: {item!r}")
    return cells


# -- fields ------------------------------------------------------------------------------------


def _date(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise RulesParseError(f"report date is not YYYY-MM-DD: {value!r}") from exc


def _number(value: str, what: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise RulesParseError(f"{what} is not a number: {value!r}") from exc


def _high_low(value: str) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    high, sep, low = value.partition("/")
    if not sep:
        raise RulesParseError(f"expected 'high / low': {value!r}")
    return (
        int(_number(high.strip(), "high temp")) if high.strip() else None,
        int(_number(low.strip(), "low temp")) if low.strip() else None,
    )


def _manpower(lines: list[str]) -> list[dict]:
    rows = []
    for line in lines:
        if set(line) <= {"-", "="}:
            continue
        company, trade, headcount, hours = _cells(line, 4, 4)
        if company.lower() in {"company", "total"}:
            continue
        rows.append(
            {
                "company": company,
                "trade": trade,
                "headcount": int(_number(headcount, "headcount")),
                "hours": _number(hours, "hours"),
            }
        )
    return rows


def _work_item(item: str) -> dict:
    match = _WORK_ITEM.match(item)
    if not match:
        raise RulesParseError(f"work item is not '[area] (trade) description': {item!r}")
    return match.groupdict()


def _delay(item: str) -> dict:
    cells = _cells(item, 3, 4)
    hours = _HOURS.match(cells[1])
    if not hours:
        raise RulesParseError(f"delay hours not like '2 hrs': {cells[1]!r}")
    rfi_no = None
    if len(cells) == 4:
        rfi = _RFI.match(cells[2])
        if not rfi:
            raise RulesParseError(f"expected 'RFI #n': {cells[2]!r}")
        rfi_no = int(rfi["n"])
    return {
        "cause": cells[0].lower(),
        "hours": float(hours["n"]),
        "rfi_no": rfi_no,
        "description": cells[-1],
    }


def _notes(lines: list[str]) -> str:
    text = " ".join(lines).strip()
    return "" if text == "None" else text
