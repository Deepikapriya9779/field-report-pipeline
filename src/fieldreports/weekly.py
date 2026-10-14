"""Weekly summary: every table and number comes from SQL; the LLM writes only one paragraph.

The narrative gets the computed facts, never the raw reports, and is rejected if it contains a
number that isn't in those facts. When it's rejected (or the LLM is skipped), a template
paragraph built from the same facts is used, and the report says which one you're reading.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

from fieldreports import queries
from fieldreports.llm import ClaudeNarrator

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def fmt(n: float | int | None) -> str:
    if n is None:
        return ""
    return f"{n:,.0f}" if float(n).is_integer() else f"{n:,.1f}"


def _day(iso: str) -> str:
    return f"{date.fromisoformat(iso):%a %b} {date.fromisoformat(iso).day}"


def gather(conn: sqlite3.Connection, week: date, project: str) -> dict:
    """All the week's numbers, as plain JSON-able data. This is all the narrator ever sees."""
    start, end = queries.week_range(week)
    daily = queries.listing(conn, "daily_log", week)
    by_sub = queries.manhours(conn, "sub", week)
    by_cause = queries.delays(conn, "cause", week)
    reported = {d["report_date"] for d in daily}
    weekdays = [(week + timedelta(days=i)).isoformat() for i in range(5)]
    return {
        "project": project,
        "week_start": start,
        "week_end": end,
        "reports": len(daily),
        "weekdays_without_report": [d for d in weekdays if d not in reported],
        "total_man_hours": sum(d["man_hours"] for d in daily),
        "peak_headcount": max((d["headcount"] for d in daily), default=0),
        "man_hours_by_sub": by_sub,
        "daily": daily,
        "total_delay_hours": sum(c["hours"] for c in by_cause),
        "delay_hours_by_cause": by_cause,
        "delay_events": queries.listing(conn, "delay_events", week),
        "weather_days": [d["report_date"] for d in daily if d["weather_delay_hours"] > 0],
        "safety_events": queries.listing(conn, "safety_events", week),
        "inspections": queries.listing(conn, "inspections", week),
        "deliveries": queries.listing(conn, "deliveries", week),
        "visitors": queries.listing(conn, "visitors", week),
    }


# -- narrative ---------------------------------------------------------------------------------


def _cause(cause: str) -> str:
    return "RFI" if cause == "rfi" else cause.replace("_", " ")


def template_narrative(f: dict) -> str:
    if not f["reports"]:
        return "No daily reports were loaded for this week."
    top = f["man_hours_by_sub"][0]
    parts = [
        f"{f['reports']} daily reports logged {fmt(f['total_man_hours'])} man-hours across "
        f"{len(f['man_hours_by_sub'])} companies, led by {top['company']} with "
        f"{fmt(top['man_hours'])}."
    ]
    if f["delay_hours_by_cause"]:
        causes = ", ".join(
            f"{_cause(c['cause'])} {fmt(c['hours'])}" for c in f["delay_hours_by_cause"]
        )
        parts.append(f"Reported delays totaled {fmt(f['total_delay_hours'])} hours ({causes}).")
    else:
        parts.append("No delays were reported.")
    failed = [i for i in f["inspections"] if i["result"] != "pass"]
    if failed:
        detail = "; ".join(f"{i['type']} ({i['result']})" for i in failed)
        parts.append(
            f"{len(failed)} of {len(f['inspections'])} inspections did not pass: {detail}."
        )
    elif f["inspections"]:
        parts.append(f"All {len(f['inspections'])} inspections passed.")
    if f["safety_events"]:
        types = ", ".join(s["type"].replace("_", " ") for s in f["safety_events"])
        parts.append(f"Safety events: {types}.")
    if f["weekdays_without_report"]:
        parts.append(f"No report on file for {len(f['weekdays_without_report'])} weekday(s).")
    return " ".join(parts)


def allowed_numbers(facts: dict) -> set[float]:
    text = repr(facts)
    return {float(n.replace(",", "")) for n in _NUMBER.findall(text)}


def narrative_problem(text: str, facts: dict) -> str | None:
    """Why an LLM narrative can't be used, or None if it can."""
    if "\n\n" in text.strip() or text.lstrip().startswith(("#", "-", "*")):
        return "not a single plain paragraph"
    allowed = allowed_numbers(facts)
    for raw in _NUMBER.findall(text):
        if float(raw.replace(",", "")) not in allowed:
            return f"number {raw} is not in the week's facts"
    return None


# -- markdown ----------------------------------------------------------------------------------


def _table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(out)


def render(f: dict, narrative: str, source: str) -> str:
    lines = [
        f"# Weekly field summary: {f['project']}",
        "",
        f"**Week of {_day(f['week_start'])} to {_day(f['week_end'])}, {f['week_end'][:4]}** · "
        f"{f['reports']} daily reports",
        "",
        "## Summary",
        "",
        narrative,
        "",
        f"_{source}_",
        "",
        "## Man-hours by company",
        "",
        "Man-hours = headcount x hours per worker, from each day's manpower log.",
        "",
    ]
    rows = [
        [s["company"], s["trade"], s["days_on_site"], s["worker_days"], fmt(s["man_hours"])]
        for s in f["man_hours_by_sub"]
    ]
    rows.append(["**Total**", "", "", "", f"**{fmt(f['total_man_hours'])}**"])
    lines += [_table(["Company", "Trade", "Days", "Worker-days", "Man-hours"], rows), ""]

    lines += ["## Daily log", ""]
    rows = [
        [
            _day(d["report_date"]),
            d["superintendent"],
            d["headcount"],
            fmt(d["man_hours"]),
            f"{d['weather_conditions']}, {d['temp_high_f']}/{d['temp_low_f']} F",
            fmt(d["weather_delay_hours"]) if d["weather_delay_hours"] else "",
            d["extraction_method"],
        ]
        for d in f["daily"]
    ]
    headers = ["Day", "Superintendent", "Headcount", "Man-hours", "Weather (hi/lo)",
               "Weather delay hrs", "Extracted by"]  # fmt: skip
    lines += [_table(headers, rows), ""]
    if f["weekdays_without_report"]:
        missing = ", ".join(_day(d) for d in f["weekdays_without_report"])
        lines += [f"**No daily report on file for:** {missing}.", ""]

    lines += ["## Delays", ""]
    if f["delay_events"]:
        rows = [[c["cause"], c["events"], fmt(c["hours"]), c["rfis"]] for c in
                f["delay_hours_by_cause"]]  # fmt: skip
        lines += [_table(["Cause", "Events", "Hours", "RFIs"], rows), ""]
        rows = [
            [_day(d["report_date"]), d["cause"], fmt(d["hours"]), d["rfi_no"] or "",
             d["description"]]
            for d in f["delay_events"]
        ]  # fmt: skip
        lines += [_table(["Day", "Cause", "Hours", "RFI", "Description"], rows), ""]
        lines += [
            "Delay hours are clock hours of stoppage as each superintendent reported them, not "
            "crew-hours. Delays on the same day can overlap and are not de-duplicated.",
            "",
        ]
    else:
        lines += ["No delays reported.", ""]

    lines += ["## Safety", ""]
    if f["safety_events"]:
        rows = [[_day(s["report_date"]), s["type"].replace("_", " "), s["description"]]
                for s in f["safety_events"]]  # fmt: skip
        lines += [_table(["Day", "Type", "Description"], rows), ""]
    else:
        lines += ["No safety events reported.", ""]

    lines += ["## Inspections", ""]
    if f["inspections"]:
        rows = [
            [_day(i["report_date"]), i["type"], i["inspector_agency"],
             i["result"].upper() if i["result"] != "pass" else "pass", i["notes"]]
            for i in f["inspections"]
        ]  # fmt: skip
        lines += [_table(["Day", "Inspection", "Agency", "Result", "Notes"], rows), ""]
    else:
        lines += ["No inspections reported.", ""]

    lines += ["## Deliveries", ""]
    if f["deliveries"]:
        rows = [[_day(d["report_date"]), d["supplier"], d["material"], d["notes"]]
                for d in f["deliveries"]]  # fmt: skip
        lines += [_table(["Day", "Supplier", "Material", "Notes"], rows), ""]
    else:
        lines += ["No deliveries reported.", ""]

    if f["visitors"]:
        lines += ["## Visitors", ""]
        lines += [f"- {_day(v['report_date'])}: {v['name']}" for v in f["visitors"]] + [""]
    return "\n".join(lines)


def build_weekly(
    conn: sqlite3.Connection, week: date, project: str, narrator: ClaudeNarrator | None = None
) -> tuple[str, str]:
    """Return (markdown, narrative source). Raises FixtureError in mock mode if not recorded."""
    facts = gather(conn, week, project)
    template = template_narrative(facts)
    if narrator is None or not facts["reports"]:
        return render(facts, template, "Template summary built from the tables below."), "template"

    result = narrator.write(facts)
    problem = result.error or narrative_problem(result.text or "", facts)
    if problem:
        source = f"Template summary: the LLM draft was rejected ({problem})."
        return render(facts, template, source), "template"
    source = (
        f"Written by {narrator.model} from the tables below; every number in it was checked "
        "against them."
    )
    return render(facts, result.text, source), "llm"
