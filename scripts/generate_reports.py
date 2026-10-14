"""Generate the fictional Harbor Point MOB daily reports.

Everything is fictional: project, people, companies, agencies. Emails use the reserved
``.example`` domain.

How it works: build the *truth* for each working day as a ``DailyReport`` first, then render it
in the writer's style. Dana Okafor fills in the company template (.txt and two .pdf). Marcus
Reyes, the assistant super, writes free-form notes (.txt, .eml and one .pdf). The gold files for
the eval are the truth for 12 of those reports, so the gold and the text come from one source.
Rendering can still lose or garble facts, which is why every gold file was then checked by hand
against its rendered text.

Run:  python scripts/generate_reports.py      (deterministic: same seed, same bytes)
Needs the dev extra (fpdf2) for the PDFs.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

from fpdf import FPDF

from fieldreports.roster import Roster
from fieldreports.schema import (
    DailyReport,
    Delay,
    DelayCause,
    Delivery,
    Inspection,
    InspectionResult,
    ManpowerEntry,
    SafetyEvent,
    SafetyType,
    Weather,
    WorkItem,
)

ROOT = Path(__file__).resolve().parents[1]
RAW, GOLD = ROOT / "data" / "raw", ROOT / "data" / "gold"
SEED = 2417
ROSTER = Roster.load()
PROJECT = ROSTER.project
DANA, MARCUS = "Dana Okafor", "Marcus Reyes"

START, END = date(2026, 8, 31), date(2026, 10, 9)
LABOR_DAY, MAKEUP_SATURDAY = date(2026, 9, 7), date(2026, 9, 26)

D = date  # shorthand for the event tables below
CITY = "City of Harbor Point Building Dept"
FIRE = "Harbor Point Fire Marshal"
SPECIAL = "Coastal Testing & Inspection (special inspector)"
OWNER_REP = "Lena Park (Harbor Health, owner's rep)"
ARCHITECT = "Priya Sethi (Linework Architects)"

# -- who writes which day, in which format ---------------------------------------------------
MARCUS_FORMAT = {
    D(2026, 9, 4): "eml",
    D(2026, 9, 11): "txt",
    D(2026, 9, 18): "eml",
    D(2026, 9, 25): "txt",
    D(2026, 9, 26): "txt",
    D(2026, 9, 28): "eml",
    D(2026, 9, 29): "txt",
    D(2026, 9, 30): "eml",
    D(2026, 10, 1): "pdf",
    D(2026, 10, 2): "txt",
    D(2026, 10, 9): "eml",
}
DANA_PDF = {D(2026, 9, 8), D(2026, 9, 23)}

GOLD_DAYS = [
    D(2026, 9, 2), D(2026, 9, 16), D(2026, 9, 22),   # template .txt
    D(2026, 9, 11), D(2026, 9, 25), D(2026, 9, 29),  # free-form .txt
    D(2026, 9, 4), D(2026, 9, 18), D(2026, 9, 30),   # .eml
    D(2026, 9, 8), D(2026, 9, 23), D(2026, 10, 1),   # .pdf (2 template, 1 free-form)
]  # fmt: skip

# -- crews: base headcount by week (week 0 starts Mon 8/31) -----------------------------------
CREW_BY_WEEK = {
    "laborers": [3, 3, 3, 3, 3, 3],
    "drywall": [6, 8, 10, 10, 12, 12],
    "electrical": [6, 6, 7, 8, 8, 8],
    "hvac": [5, 5, 6, 6, 6, 5],
    "plumbing": [4, 4, 4, 3, 3, 3],
    "fire_sprinkler": [3, 4, 4, 4, 3, 2],
    "roofing": [6, 6, 6, 6, 4, 0],
    "curtain_wall": [0, 4, 5, 5, 5, 5],
    "sitework": [4, 4, 0, 0, 3, 3],
}
SUB_BY_TRADE = {s.trade: s.company for s in ROSTER.subs}
EXTERIOR = {"roofing", "curtain_wall", "sitework"}

# Per-day overrides: trade -> (headcount, hours). headcount 0 = not on site.
CREW_OVERRIDES: dict[date, dict[str, tuple[int, float]]] = {
    D(2026, 9, 22): {"roofing": (0, 0), "curtain_wall": (0, 0)},  # rained out, called off
    D(2026, 9, 23): {"roofing": (6, 4), "curtain_wall": (5, 4)},  # started 11:30 after rain
    D(2026, 9, 25): {"curtain_wall": (5, 6)},  # glaziers left at 1
    D(2026, 9, 29): {"electrical": (8, 8)},  # idle 1.5 hrs on a dead lift, still paid 8
    D(2026, 9, 30): {"electrical": (8, 6)},  # crew pulled to another job at 1 PM
}
SATURDAY_CREW = {"laborers": (2, 8), "roofing": (6, 8), "curtain_wall": (5, 8)}

# -- work phrases by trade (early weeks / later weeks) and the areas each trade works in -----
WORK = {
    "laborers": (
        ["Cleaned L1 and L2, loaded dumpster", "Set up temporary protection at stair 1"],
        ["Swept and cleaned all floors, loaded dumpster", "Moved board stock to work areas"],
    ),
    "drywall": (
        ["Framed metal stud partitions", "Installed top track and in-wall blocking"],
        ["Hung 5/8 board on corridor walls", "Taped and first coat on hung walls"],
    ),
    "electrical": (
        ["Ran conduit for branch circuits", "Set boxes and ran MC cable in exam rooms"],
        ["Pulled wire for lighting circuits", "Terminated devices in exam rooms"],
    ),
    "hvac": (
        ["Hung main supply duct", "Installed VAV boxes"],
        ["Hung branch duct and flex to diffusers", "Insulated supply duct"],
    ),
    "plumbing": (
        ["Roughed in restroom core waste and vent", "Set carriers for wall-hung fixtures"],
        ["Ran domestic water branches", "Pressure tested domestic water"],
    ),
    "fire_sprinkler": (
        ["Installed sprinkler main", "Ran branch lines"],
        ["Dropped sprinkler heads to ceiling height", "Installed branch lines and hangers"],
    ),
    "roofing": (
        ["Installed membrane on main roof", "Flashed RTU curbs"],
        ["Installed edge metal and coping", "Flashed roof drains and penetrations"],
    ),
    "curtain_wall": (
        ["Set curtain wall anchors", "Set curtain wall units"],
        ["Set curtain wall units", "Glazed and sealed vision panels"],
    ),
    "sitework": (
        ["Graded east parking area", "Installed storm drain inlet protection"],
        ["Prepped subgrade for curb and gutter", "Graded east parking area"],
    ),
}
INTERIOR_AREAS = ["L1 east", "L1 west", "L2 east", "L2 west", "L3 east", "L3 west"]
AREAS = {
    "roofing": ["Main roof"],
    "curtain_wall": ["North elevation", "South elevation", "East elevation"],
    "sitework": ["East parking lot"],
}

# -- scripted events ---------------------------------------------------------------------------
DELAYS: dict[date, list[Delay]] = {
    D(2026, 9, 3): [
        Delay(
            cause=DelayCause.MATERIAL,
            hours=2,
            description="Sprinkler pipe for L2 short 40 ft of 2 in. main; crew moved to L1 branch lines",
        )
    ],
    D(2026, 9, 4): [
        Delay(
            cause=DelayCause.EQUIPMENT,
            hours=1.5,
            description="Boom lift would not start; roofers waited on mechanic",
        )
    ],
    D(2026, 9, 10): [
        Delay(
            cause=DelayCause.INSPECTION,
            hours=3,
            description="Fire marshal moved L1 hydro test from AM to PM; sprinkler crew could not proceed",
        )
    ],
    D(2026, 9, 16): [
        Delay(
            cause=DelayCause.RFI,
            hours=3,
            rfi_no=11,
            description="Ceiling height conflict at L2 corridor, duct vs sprinkler main",
        )
    ],
    D(2026, 9, 17): [
        Delay(
            cause=DelayCause.MATERIAL,
            hours=4,
            description="Curtain wall anchors short-shipped; glaziers moved to staging",
        )
    ],
    D(2026, 9, 22): [
        Delay(
            cause=DelayCause.WEATHER,
            hours=8,
            description="Heavy rain all day; roofing and curtain wall did not work",
        )
    ],
    D(2026, 9, 23): [
        Delay(
            cause=DelayCause.WEATHER,
            hours=4,
            description="Rain until 11 AM; roofing and curtain wall started at 11:30",
        )
    ],
    D(2026, 9, 24): [
        Delay(
            cause=DelayCause.OWNER_CHANGE,
            hours=2,
            description="Owner directive ASI-07 relocates exam room 214 wall; drywall held on L2 east",
        )
    ],
    D(2026, 9, 25): [
        Delay(
            cause=DelayCause.RFI,
            hours=2,
            rfi_no=14,
            description="Waiting on RFI 14 response, sprinkler head layout at L3 lobby ceiling",
        )
    ],
    D(2026, 9, 29): [
        Delay(
            cause=DelayCause.EQUIPMENT,
            hours=1.5,
            description="Scissor lift dead battery; elec lost time on L2",
        )
    ],
    D(2026, 9, 30): [
        Delay(
            cause=DelayCause.LABOR,
            hours=2.5,
            description="Volt Line pulled their crew to another job at 1 PM; L2 wire pull stopped",
        )
    ],
    D(2026, 10, 1): [
        Delay(
            cause=DelayCause.MATERIAL,
            hours=1,
            description="6 door frames delivered wrong hand; framers re-sequenced",
        )
    ],
    D(2026, 10, 8): [
        Delay(
            cause=DelayCause.RFI,
            hours=1.5,
            rfi_no=17,
            description="Waiting on RFI 17 response, L3 soffit framing detail",
        )
    ],
}

SAFETY: dict[date, list[SafetyEvent]] = {
    D(2026, 9, 2): [
        SafetyEvent(
            type=SafetyType.OBSERVATION,
            description="Debris at L2 stair landing; laborers cleaned it up",
        )
    ],
    D(2026, 9, 9): [
        SafetyEvent(
            type=SafetyType.NEAR_MISS,
            description="2x4 block fell from L3 deck edge to grade, nobody below; area barricaded "
            "and toe boards added",
        )
    ],
    D(2026, 9, 11): [
        SafetyEvent(
            type=SafetyType.FIRST_AID,
            description="Keystone framer cut hand on stud edge; cleaned and bandaged on site, "
            "back to work",
        )
    ],
    D(2026, 9, 17): [
        SafetyEvent(
            type=SafetyType.OBSERVATION,
            description="Two workers without safety glasses at L3; corrected on the spot",
        )
    ],
    D(2026, 9, 25): [
        SafetyEvent(
            type=SafetyType.OBSERVATION,
            description="Ladder missing a foot found on L2; tagged out and removed",
        )
    ],
    D(2026, 10, 2): [
        SafetyEvent(
            type=SafetyType.NEAR_MISS,
            description="Ceiling grid bundle slid off scissor lift deck on L1, no injury",
        )
    ],
    D(2026, 10, 7): [
        SafetyEvent(
            type=SafetyType.RECORDABLE,
            description="Volt Line electrician strained lower back lifting conduit; seen at "
            "occupational clinic and given prescription medication",
        )
    ],
}

INSPECTIONS: dict[date, list[Inspection]] = {
    D(2026, 9, 1): [
        Inspection(
            type="Plumbing rough-in, L1 restroom core",
            inspector_agency=CITY,
            result=InspectionResult.PASS,
            notes="",
        )
    ],
    D(2026, 9, 8): [
        Inspection(
            type="In-wall rough-in L1",
            inspector_agency=CITY,
            result=InspectionResult.PASS,
            notes="OK to close L1 walls",
        )
    ],
    D(2026, 9, 14): [
        Inspection(
            type="Sprinkler hydrostatic test L1",
            inspector_agency=FIRE,
            result=InspectionResult.FAIL,
            notes="Leaks at 2 fittings in east wing; retest required",
        )
    ],
    D(2026, 9, 16): [
        Inspection(
            type="Framing L2",
            inspector_agency=CITY,
            result=InspectionResult.PARTIAL,
            notes="L2 west approved; L2 east needs blocking at grab bar locations",
        )
    ],
    D(2026, 9, 18): [
        Inspection(
            type="Sprinkler hydrostatic re-test L1",
            inspector_agency=FIRE,
            result=InspectionResult.PASS,
            notes="Held 200 psi for 2 hours",
        )
    ],
    D(2026, 9, 21): [
        Inspection(
            type="Firestopping L1 penetrations",
            inspector_agency=SPECIAL,
            result=InspectionResult.PASS,
            notes="",
        )
    ],
    D(2026, 9, 24): [
        Inspection(
            type="Above-ceiling L1 east",
            inspector_agency=CITY,
            result=InspectionResult.PASS,
            notes="",
        )
    ],
    D(2026, 9, 30): [
        Inspection(
            type="Above-ceiling L1 west",
            inspector_agency=CITY,
            result=InspectionResult.PASS,
            notes="",
        )
    ],
    D(2026, 10, 5): [
        Inspection(
            type="Sprinkler hydrostatic test L2",
            inspector_agency=FIRE,
            result=InspectionResult.PASS,
            notes="",
        )
    ],
    D(2026, 10, 8): [
        Inspection(type="Framing L3", inspector_agency=CITY, result=InspectionResult.PASS, notes="")
    ],
}

DELIVERIES: dict[date, list[Delivery]] = {
    D(2026, 8, 31): [
        Delivery(
            supplier="Ironbridge Supply",
            material="Metal stud framing package, L1-L2",
            notes="Staged on L1 slab, west end",
        )
    ],
    D(2026, 9, 2): [
        Delivery(
            supplier="Coastal Gypsum Supply",
            material="5/8 in. Type X gypsum board, 12 bunks",
            notes="Boom-loaded to L2 and L3",
        )
    ],
    D(2026, 9, 4): [
        Delivery(
            supplier="Summit Electrical Supply", material="Wire and MC cable, 14 reels", notes=""
        )
    ],
    D(2026, 9, 8): [
        Delivery(
            supplier="Northwind HVAC Equipment",
            material="RTU-1 and RTU-2 rooftop units",
            notes="Set by crane 6:30-9:30 AM",
        )
    ],
    D(2026, 9, 11): [
        Delivery(
            supplier="Coastal Gypsum Supply",
            material="5/8 in. Type X gypsum board, 8 bunks",
            notes="",
        )
    ],
    D(2026, 9, 15): [
        Delivery(
            supplier="Glassline Curtainwall",
            material="Curtain wall units, north elevation (18)",
            notes="Stored in east laydown",
        )
    ],
    D(2026, 9, 21): [
        Delivery(
            supplier="Summit Electrical Supply",
            material="Main switchgear MSB-1",
            notes="Set in L1 electrical room with forklift",
        )
    ],
    D(2026, 9, 25): [
        Delivery(
            supplier="Coastal Gypsum Supply",
            material="5/8 in. Type X gypsum board, 8 bunks",
            notes="",
        )
    ],
    D(2026, 9, 28): [
        Delivery(
            supplier="Ceiling Systems Direct",
            material="Acoustical ceiling grid and tile, L1",
            notes="",
        )
    ],
    D(2026, 10, 1): [
        Delivery(
            supplier="Doorway Hardware Co.",
            material="Hollow metal door frames, L1-L2 (24)",
            notes="6 frames wrong hand, sent back",
        )
    ],
    D(2026, 10, 6): [
        Delivery(
            supplier="Coastal Gypsum Supply",
            material="5/8 in. Type X gypsum board, 10 bunks",
            notes="",
        )
    ],
}

VISITORS: dict[date, list[str]] = {
    D(2026, 8, 31): [OWNER_REP],
    D(2026, 9, 4): [ARCHITECT],
    D(2026, 9, 14): [OWNER_REP],
    D(2026, 9, 21): [OWNER_REP, ARCHITECT],
    D(2026, 10, 2): [OWNER_REP],
    D(2026, 10, 7): [OWNER_REP],
}

NOTES: dict[date, str] = {
    D(2026, 8, 31): "Start of interior rough-in. Roofing continuing on main roof membrane.",
    D(2026, 9, 8): "RTU pick went well; crane off site by 10.",
    D(2026, 9, 14): "Sprinkler retest to be scheduled after Redline repairs fittings.",
    D(2026, 9, 22): "Interior trades worked full day. Pumped water from elevator pit.",
    D(2026, 9, 26): "Saturday make-up day for rain on 9/22 and 9/23. Exterior trades only.",
    D(2026, 10, 9): "Roof complete except punch. Sitework starts curb and gutter Monday.",
}

RAIN = {
    D(2026, 9, 22): ("Heavy rain", 66, 60),
    D(2026, 9, 23): ("Rain until 11 AM, then overcast", 70, 61),
}
DRY_CONDITIONS = ["Clear", "Sunny", "Partly cloudy", "Overcast", "Hot and humid"]


def working_days() -> list[date]:
    days, d = [], START
    while d <= END:
        if d.weekday() < 5 and d != LABOR_DAY:
            days.append(d)
        d += timedelta(days=1)
    return sorted([*days, MAKEUP_SATURDAY])


def build_truth(day: date) -> DailyReport:
    rng = random.Random(f"{SEED}-{day}")
    week = (day - START).days // 7
    writer = MARCUS if day in MARCUS_FORMAT else DANA

    if day in RAIN:
        conditions, high, low = RAIN[day]
    else:
        conditions = rng.choice(DRY_CONDITIONS)
        high = 88 - 2 * week + rng.randint(-3, 3)
        low = high - rng.randint(12, 16)

    if day == MAKEUP_SATURDAY:
        crew_plan = dict(SATURDAY_CREW)
    else:
        crew_plan = {}
        for trade, by_week in CREW_BY_WEEK.items():
            base = by_week[week]
            if base:
                jitter = rng.choice([-1, 0, 0, 1]) if base >= 4 else 0
                hours = 10.0 if trade == "drywall" and week >= 4 else 8.0  # Keystone on 10s
                crew_plan[trade] = (base + jitter, hours)
        crew_plan.update(CREW_OVERRIDES.get(day, {}))

    manpower = [
        ManpowerEntry(company=SUB_BY_TRADE[t], trade=t, headcount=n, hours=h)
        for t, (n, h) in crew_plan.items()
        if n > 0
    ]

    on_site = [m.trade for m in manpower]
    work = []
    for trade in rng.sample(on_site, k=min(len(on_site), rng.randint(3, 5))):
        phrases = WORK[trade][0 if week < 3 else 1]
        area = rng.choice(AREAS.get(trade, INTERIOR_AREAS))
        work.append(WorkItem(area=area, trade=trade, description=rng.choice(phrases)))

    delays = DELAYS.get(day, [])
    weather_hours = sum(d.hours for d in delays if d.cause is DelayCause.WEATHER)
    return DailyReport(
        project=PROJECT,
        report_date=day,
        superintendent=writer,
        weather=Weather(
            conditions=conditions, temp_high_f=high, temp_low_f=low,
            weather_delay_hours=weather_hours,
        ),
        manpower=manpower,
        work_completed=work,
        delays=delays,
        safety_events=SAFETY.get(day, []),
        inspections=INSPECTIONS.get(day, []),
        deliveries=DELIVERIES.get(day, []),
        visitors=VISITORS.get(day, []),
        notes=NOTES.get(day, ""),
    )  # fmt: skip


# -- renderers ---------------------------------------------------------------------------------


def render_template(r: DailyReport, report_no: int, blank_date: bool = False) -> str:
    """Dana's style: the company DR-1 form, every section present."""
    w = r.weather
    lines = [
        "BAYLINE BUILDERS - SUPERINTENDENT DAILY REPORT",
        "Form DR-1 rev 3",
        "=" * 64,
        f"Project:          {r.project}",
        "Job No.:          2417",
        f"Report Date:      {'' if blank_date else r.report_date.isoformat()}",
        f"Superintendent:   {r.superintendent}",
        f"Report No.:       {report_no}",
        "",
        "WEATHER",
        f"Conditions:       {w.conditions}",
        f"High / Low (F):   {w.temp_high_f} / {w.temp_low_f}",
        f"Weather Delay (hrs): {w.weather_delay_hours:g}",
        "",
        "MANPOWER",
        f"{'Company':<32}| {'Trade':<15}| {'Headcount':<10}| Hours",
        "-" * 64,
    ]
    for m in r.manpower:
        lines.append(f"{m.company:<32}| {m.trade:<15}| {m.headcount:<10}| {m.hours:g}")
    total = sum(m.headcount for m in r.manpower)
    lines += [f"{'TOTAL':<32}| {'':<15}| {total:<10}|", "", "WORK COMPLETED"]
    lines += [f"- [{i.area}] ({i.trade}) {i.description}" for i in r.work_completed] or ["- None"]

    lines += ["", "DELAYS"]
    for d in r.delays:
        rfi = f" | RFI #{d.rfi_no}" if d.rfi_no is not None else ""
        lines.append(f"- {d.cause.value} | {d.hours:g} hrs{rfi} | {d.description}")
    if not r.delays:
        lines.append("- None")

    lines += ["", "SAFETY"]
    lines += [f"- {s.type.value} | {s.description}" for s in r.safety_events] or ["- None"]
    lines += ["", "INSPECTIONS"]
    lines += [
        f"- {i.type} | {i.inspector_agency} | {i.result.value} | {i.notes}".rstrip(" |")
        for i in r.inspections
    ] or ["- None"]
    lines += ["", "DELIVERIES"]
    lines += [f"- {d.supplier} | {d.material} | {d.notes}".rstrip(" |") for d in r.deliveries] or [
        "- None"
    ]
    lines += ["", "VISITORS"]
    lines += [f"- {v}" for v in r.visitors] or ["- None"]
    lines += ["", "NOTES", r.notes or "None", ""]
    return "\n".join(lines)


NICKNAMES = {
    "laborers": ["bayline labor", "gc labor", "laborers"],
    "drywall": ["keystone", "keystone drywall", "framers"],
    "electrical": ["elec", "volt line", "electricians"],
    "hvac": ["tidewater", "mech", "tidewater mech"],
    "plumbing": ["clearwater", "plumbers", "clearwater plumb"],
    "fire_sprinkler": ["redline", "sprinkler", "redline fp"],
    "roofing": ["summit roofers", "roofers", "summit"],
    "curtain_wall": ["glassline", "glaziers"],
    "sitework": ["g&g site", "gravel & grade"],
}
SHORT_TRADE = {
    "laborers": "labor",
    "drywall": "drywall",
    "electrical": "elec",
    "hvac": "mech",
    "plumbing": "plumb",
    "fire_sprinkler": "sprinkler",
    "roofing": "roof",
    "curtain_wall": "glazing",
    "sitework": "site",
}
RESULT_WORDS = {"pass": "passed", "fail": "FAILED", "partial": "partial pass"}
SAFETY_WORDS = {
    "near_miss": "near miss",
    "first_aid": "first aid",
    "recordable": "RECORDABLE",
    "observation": "safety obs",
}


def render_freeform(r: DailyReport, rng: random.Random) -> str:
    """Marcus's style: short lines, abbreviations, crews 'elec 6 guys', default 8 hr day."""
    d = r.report_date
    mdy = f"{d.month}/{d.day}/{d.year % 100}"
    w = r.weather
    out = [rng.choice([f"Harbor Pt MOB - daily {mdy}", f"HP MOB daily {mdy}"]), "Marcus R.", ""]
    out.append("all crews 7-3:30 (8 hrs) unless noted")
    wx = f"wx: {w.conditions.lower()}, hi {w.temp_high_f} lo {w.temp_low_f}"
    out += [wx, ""]

    out.append(rng.choice(["guys on site:", "crews:", "manpower:"]))
    for m in r.manpower:
        nick = rng.choice(NICKNAMES[m.trade])
        count = rng.choice([f"{m.headcount} guys", f"x{m.headcount}", f"- {m.headcount}"])
        line = f"{nick} {count}"
        if m.hours == 6:
            line += " - left at 1 (6 hrs)"
        elif m.hours != 8:
            line += f" ({m.hours:g} hrs)"
        out.append(line)

    out += ["", "done today:"]
    for i in r.work_completed:
        out.append(f"- {SHORT_TRADE[i.trade]}: {i.description.lower()} {i.area}")

    issues = []
    for dl in r.delays:
        if dl.cause is DelayCause.RFI:
            issues.append(f"lost {dl.hours:g} hrs - {dl.description.lower()}")
        else:
            issues.append(f"{dl.description.lower()} ({dl.hours:g} hrs lost)")
    out += ["", "issues:"] + ([f"- {i}" for i in issues] or ["- none"])

    for s in r.safety_events:
        out.append(f"{SAFETY_WORDS[s.type.value]}: {s.description.lower()}")
    for i in r.inspections:
        note = f". {i.notes}" if i.notes else ""
        out.append(
            f"inspection: {i.type} w/ {i.inspector_agency} - {RESULT_WORDS[i.result.value]}{note}"
        )
    for dv in r.deliveries:
        note = f" ({dv.notes.lower()})" if dv.notes else ""
        out.append(f"delivered: {dv.material.lower()} from {dv.supplier}{note}")
    if r.visitors:
        out.append("visitors: " + "; ".join(r.visitors))
    if r.notes:
        out.append(f"other: {r.notes}")
    return "\n".join(out) + "\n"


def render_email(r: DailyReport, body: str, rng: random.Random) -> bytes:
    d = r.report_date
    msg = EmailMessage()
    msg["From"] = "Marcus Reyes <mreyes@bayline.example>"
    msg["To"] = "Tom Alvarez <talvarez@bayline.example>"
    msg["Subject"] = f"Daily - Harbor Pt MOB - {d.month}/{d.day}/{d.year % 100}"
    # One report is sent after midnight: the header date is the next day. This is why the
    # pipeline never takes the report date from the email header.
    sent = datetime(
        d.year, d.month, d.day, 17, rng.randint(5, 55), tzinfo=timezone(timedelta(hours=-4))
    )
    if d == date(2026, 9, 30):
        sent += timedelta(hours=7, minutes=10)
    msg["Date"] = format_datetime(sent)
    msg["Message-ID"] = f"<daily-{d.isoformat()}@bayline.example>"
    msg.set_content(f"Tom,\n\ndaily below.\n\n{body}\n-M\n")
    return bytes(msg)


def write_pdf(text: str, path: Path, monospace: bool) -> None:
    pdf = FPDF(format="letter")
    pdf.set_creation_date(datetime(2026, 10, 10, tzinfo=UTC))  # deterministic bytes
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Courier" if monospace else "Helvetica", size=8 if monospace else 10)
    for line in text.splitlines():
        pdf.multi_cell(0, 4 if monospace else 5, line or " ", new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(path))


def garble(text: str, rng: random.Random, rate: float = 0.45) -> str:
    """Simulate a bad OCR pass on a crumpled, coffee-stained scan."""
    noise = "#@$%&*~^0123456789"
    return "".join(rng.choice(noise) if ch.isalnum() and rng.random() < rate else ch for ch in text)


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    GOLD.mkdir(parents=True, exist_ok=True)
    for old in [*RAW.iterdir(), *GOLD.iterdir()]:
        old.unlink()

    written: dict[date, str] = {}
    for n, day in enumerate(working_days(), start=1):
        truth = build_truth(day)
        rng = random.Random(f"{SEED}-render-{day}")
        stamp = f"{day.month}-{day.day}"
        if day in MARCUS_FORMAT:
            fmt = MARCUS_FORMAT[day]
            body = render_freeform(truth, rng)
            if fmt == "txt":
                name = f"daily {stamp}.txt"
                (RAW / name).write_text(body, encoding="utf-8", newline="\n")
            elif fmt == "eml":
                name = f"Daily - Harbor Pt MOB - {stamp}.eml"
                (RAW / name).write_bytes(render_email(truth, body, rng))
            else:
                name = f"daily notes {stamp}.pdf"
                write_pdf(body, RAW / name, monospace=False)
        else:
            text = render_template(truth, report_no=n)
            if day in DANA_PDF:
                name = f"DR_{day.isoformat()}_HarborPoint.pdf"
                write_pdf(text, RAW / name, monospace=True)
            else:
                name = f"DR_{day.isoformat()}_HarborPoint.txt"
                (RAW / name).write_text(text, encoding="utf-8", newline="\n")
        written[day] = name

        if day in GOLD_DAYS:
            gold = {"source_file": name, "report": truth.model_dump(mode="json", exclude={
                "source_file", "extraction_method", "confidence"})}  # fmt: skip
            (GOLD / f"{day.isoformat()}.json").write_text(
                json.dumps(gold, indent=2) + "\n", encoding="utf-8", newline="\n"
            )

    # Broken report 1: Dana's unsaved draft of 9/16, date field left blank.
    draft = render_template(build_truth(D(2026, 9, 16)), report_no=13, blank_date=True)
    (RAW / "DR_HarborPoint_draft.txt").write_text(draft, encoding="utf-8", newline="\n")
    # Broken report 2: an OCR'd scan of a crumpled paper copy.
    scan = garble(render_template(build_truth(D(2026, 9, 9)), report_no=7), random.Random(SEED))
    (RAW / "SCAN0047.txt").write_text(scan, encoding="utf-8", newline="\n")

    print(f"wrote {len(written) + 2} files to {RAW.relative_to(ROOT)} "
          f"({len(written)} reports + 2 broken), {len(GOLD_DAYS)} gold files")  # fmt: skip


if __name__ == "__main__":
    main()
