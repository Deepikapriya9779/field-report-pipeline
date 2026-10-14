"""Field-level precision, recall and F1 per report section, against hand-checked gold reports.

How a report is scored:

1. Both the prediction and the gold are turned into items per section (a crew, a delay, a
   visitor...). Header, weather and notes are one-item sections.
2. Within a section, predicted items are matched to gold items greedily, best pair first, by
   the share of fields that agree. That way list order doesn't matter.
3. Each field of a matched pair is one fact:
   * equal                -> 1 true positive
   * both filled, unequal -> 1 false positive and 1 false negative (wrong value)
   * only gold filled     -> 1 false negative (missed)
   * only prediction      -> 1 false positive (made up)
   * both empty           -> not counted, so empty fields can't pad the score
   Unmatched items count every filled field as missed (gold) or made up (prediction).
4. Numbers, enums and canonical names must match exactly. Free-text fields (descriptions,
   areas, notes) match when the token Dice overlap is >= ``TEXT_MATCH``, because "L1 west
   corridor walls" vs "L1 west" is a wording choice, not an extraction error.

precision = TP / (TP + FP), recall = TP / (TP + FN), F1 = harmonic mean. Scores are micro
averages: every fact counts once, so a section with more facts weighs more in "all".
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from fieldreports.extract import extract
from fieldreports.llm import ClaudeExtractor, cost_usd
from fieldreports.loaders import load_text
from fieldreports.roster import Roster
from fieldreports.rules import RulesParseError, is_template, parse
from fieldreports.schema import ReportFields

ROOT = Path(__file__).resolve().parents[2]
GOLD_DIR, RAW_DIR = ROOT / "data" / "gold", ROOT / "data" / "raw"

TEXT_MATCH = 0.6
EXACT, TEXT = "exact", "text"

SECTIONS: dict[str, dict[str, str]] = {
    "header": {"project": EXACT, "report_date": EXACT, "superintendent": EXACT},
    "weather": {"conditions": TEXT, "temp_high_f": EXACT, "temp_low_f": EXACT,
                "weather_delay_hours": EXACT},
    "manpower": {"company": EXACT, "trade": EXACT, "headcount": EXACT, "hours": EXACT},
    "work_completed": {"area": TEXT, "trade": EXACT, "description": TEXT},
    "delays": {"cause": EXACT, "hours": EXACT, "rfi_no": EXACT, "description": TEXT},
    "safety_events": {"type": EXACT, "description": TEXT},
    "inspections": {"type": TEXT, "inspector_agency": TEXT, "result": EXACT, "notes": TEXT},
    "deliveries": {"supplier": TEXT, "material": TEXT, "notes": TEXT},
    "visitors": {"name": TEXT},
    "notes": {"notes": TEXT},
}  # fmt: skip

_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    def __iadd__(self, other: Counts) -> Counts:
        self.tp, self.fp, self.fn = self.tp + other.tp, self.fp + other.fp, self.fn + other.fn
        return self

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


@dataclass
class ReportScore:
    source: str
    sections: dict[str, Counts] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)  # human-readable FP/FN facts


# -- comparing values --------------------------------------------------------------------------


def _empty(v: object) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def dice(a: str, b: str) -> float:
    ta, tb = set(_TOKEN.findall(a.lower())), set(_TOKEN.findall(b.lower()))
    return 2 * len(ta & tb) / (len(ta) + len(tb)) if ta or tb else 1.0


def same(kind: str, gold: object, pred: object) -> bool:
    if kind == TEXT:
        return dice(str(gold), str(pred)) >= TEXT_MATCH
    if isinstance(gold, int | float) and isinstance(pred, int | float):
        return float(gold) == float(pred)
    return " ".join(str(gold).lower().split()) == " ".join(str(pred).lower().split())


# -- items -------------------------------------------------------------------------------------


def items(report: ReportFields | None, section: str) -> list[dict]:
    if report is None:
        return []
    data = report.model_dump(mode="json")
    if section == "header":
        return [{k: data[k] for k in SECTIONS["header"]}]
    if section == "weather":
        return [data["weather"]]
    if section == "notes":
        return [{"notes": data["notes"]}]
    if section == "visitors":
        return [{"name": v} for v in data["visitors"]]
    return data[section]


def _pair_score(fields: dict[str, str], g: dict, p: dict) -> float:
    filled = [f for f in fields if not (_empty(g.get(f)) and _empty(p.get(f)))]
    if not filled:
        return 0.0
    hits = sum(
        not _empty(g.get(f)) and not _empty(p.get(f)) and same(k, g[f], p[f])
        for f, k in fields.items()
        if f in filled
    )
    return hits / len(filled)


def score_section(section: str, gold: list[dict], pred: list[dict]) -> tuple[Counts, list[str]]:
    fields = SECTIONS[section]
    pairs = sorted(
        (
            (_pair_score(fields, g, p), gi, pi)
            for gi, g in enumerate(gold)
            for pi, p in enumerate(pred)
        ),
        key=lambda t: (-t[0], t[1], t[2]),
    )
    matched: list[tuple[int, int]] = []
    used_g, used_p = set(), set()
    for s, gi, pi in pairs:
        if s > 0 and gi not in used_g and pi not in used_p:
            matched.append((gi, pi))
            used_g.add(gi)
            used_p.add(pi)

    counts, errors = Counts(), []
    for gi, pi in matched:
        g, p = gold[gi], pred[pi]
        for f, kind in fields.items():
            gv, pv = g.get(f), p.get(f)
            if _empty(gv) and _empty(pv):
                continue
            if _empty(pv):
                counts.fn += 1
                errors.append(f"{section}.{f}: missed {gv!r}")
            elif _empty(gv):
                counts.fp += 1
                errors.append(f"{section}.{f}: extra {pv!r}")
            elif same(kind, gv, pv):
                counts.tp += 1
            else:
                counts.fp += 1
                counts.fn += 1
                errors.append(f"{section}.{f}: {pv!r}, expected {gv!r}")
    for gi in sorted(set(range(len(gold))) - used_g):
        n = sum(not _empty(v) for v in gold[gi].values())
        counts.fn += n
        if n:  # an all-empty item (e.g. blank notes) is not a fact
            errors.append(f"{section}: missed item {gold[gi]}")
    for pi in sorted(set(range(len(pred))) - used_p):
        n = sum(not _empty(v) for v in pred[pi].values())
        counts.fp += n
        if n:
            errors.append(f"{section}: extra item {pred[pi]}")
    return counts, errors


def score_report(source: str, gold: ReportFields, pred: ReportFields | None) -> ReportScore:
    result = ReportScore(source)
    for section in SECTIONS:
        counts, errors = score_section(section, items(gold, section), items(pred, section))
        result.sections[section] = counts
        result.errors += errors
    return result


def total(scores: list[ReportScore], section: str | None = None) -> Counts:
    out = Counts()
    for s in scores:
        for name, counts in s.sections.items():
            if section is None or name == section:
                out += counts
    return out


# -- running a method over the gold set --------------------------------------------------------


Predictor = Callable[[str], ReportFields | None]


def run(
    predict: Predictor, gold: list[tuple[str, str, ReportFields]], roster: Roster
) -> list[ReportScore]:
    """gold: (source file, source text, expected report). Predictions are roster-normalized,
    exactly as the pipeline does before validation."""
    scores = []
    for source, text, expected in gold:
        pred = predict(text)
        if pred is not None:
            pred, _ = roster.normalize(pred)
        scores.append(score_report(source, expected, pred))
    return scores


# -- the eval run and its report ---------------------------------------------------------------


@dataclass
class EvalRun:
    model: str
    mode: str
    template: list[str]  # gold sources that are DR-1 templates
    scores: dict[str, list[ReportScore]]  # method -> one score per gold report
    usage: list[dict]


def load_gold_set(
    gold_dir: Path = GOLD_DIR, raw_dir: Path = RAW_DIR
) -> list[tuple[str, str, ReportFields]]:
    out = []
    for path in sorted(gold_dir.glob("*.json")):
        gold = json.loads(path.read_text(encoding="utf-8"))
        text = load_text(raw_dir / gold["source_file"])
        out.append((gold["source_file"], text, ReportFields.model_validate(gold["report"])))
    return out


def evaluate(
    extractor: ClaudeExtractor,
    roster: Roster,
    methods: tuple[str, ...] = ("rules", "llm", "pipeline"),
) -> EvalRun:
    gold = load_gold_set()

    def rules(text: str) -> ReportFields | None:
        try:
            return parse(text) if is_template(text) else None
        except RulesParseError:
            return None

    predictors: dict[str, Predictor] = {
        "rules": rules,
        "llm": lambda text: extractor.extract(text).report,
        "pipeline": lambda text: extract(text, extractor).report,
    }
    scores, usage = {}, {}
    for m in methods:
        before = len(extractor.usage)
        scores[m] = run(predictors[m], gold, roster)
        usage[m] = extractor.usage[before:]
    return EvalRun(
        model=extractor.model,
        mode="",
        template=[src for src, text, _ in gold if is_template(text)],
        scores=scores,
        # "pipeline" repeats the LLM method's free-form requests, so cost one method's calls
        usage=usage.get("llm") or usage.get("pipeline") or [],
    )


def _pct(x: float) -> str:
    return f"{x:.3f}"


def _f1_or_na(c: Counts) -> str:
    """n/a when neither side has a fact in this section (nothing to get right or wrong)."""
    return _pct(c.f1) if c.tp + c.fp + c.fn else "n/a"


def render_results(ev: EvalRun) -> str:
    n = len(next(iter(ev.scores.values())))
    n_tpl = len(ev.template)
    subsets = {
        f"template ({n_tpl})": lambda s: s.source in ev.template,
        f"free-form ({n - n_tpl})": lambda s: s.source not in ev.template,
        f"all ({n})": lambda s: True,
    }
    label = {"rules": "Rules", "llm": "LLM", "pipeline": "Pipeline (rules first, LLM fallback)"}

    lines = [
        "# Extraction eval",
        "",
        f"Gold set: {n} hand-checked reports, {n_tpl} on the DR-1 template and "
        f"{n - n_tpl} free-form (.txt, .eml, .pdf). Field-level micro-averaged scores; "
        "see `src/fieldreports/evaluate.py` for the matching rules.",
        "",
        f"LLM: `{ev.model}`, mode `{ev.mode}`"
        + (" (replaying responses recorded with `LLM_MODE=record`; nothing called the API)."
           if ev.mode == "mock" else "."),
        "",
        "## Overall",
        "",
        "| Method | Reports | Precision | Recall | F1 |",
        "|---|---|---|---|---|",
    ]  # fmt: skip
    for method, scores in ev.scores.items():
        for name, keep in subsets.items():
            picked = [s for s in scores if keep(s)]
            if method == "rules" and name.startswith("all"):
                continue
            c = total(picked)
            lines.append(
                f"| {label[method]} | {name} | {_pct(c.precision)} | {_pct(c.recall)} | "
                f"{_pct(c.f1)} |"
            )
    lines += [
        "",
        "Rules return nothing for free-form reports by design (they only read the template), "
        "so their free-form row is 0. The pipeline routes template reports to the rules and "
        "everything else to the LLM, so its free-form row is the LLM's own score; the pipeline "
        "rows are what `ingest` actually does.",
        "",
    ]
    if "llm" not in ev.scores:
        lines += [
            "Not run here: the LLM on the template reports as well (the rules-vs-LLM comparison "
            "on identical inputs). It needs those responses recorded first: "
            "`fieldreports eval --record`.",
            "",
        ]

    main = "pipeline" if "pipeline" in ev.scores else next(iter(ev.scores))
    lines += [
        f"## By section: {label[main]}, all {n} reports",
        "",
        "| Section | Gold facts | Precision | Recall | F1 |",
        "|---|---|---|---|---|",
    ]
    for section in SECTIONS:
        c = total(ev.scores[main], section)
        lines.append(
            f"| {section} | {c.tp + c.fn} | {_pct(c.precision)} | {_pct(c.recall)} | {_pct(c.f1)} |"
        )

    if {"rules", "llm"} <= set(ev.scores):
        lines += [
            "",
            f"## Rules vs LLM on the same {n_tpl} template reports",
            "",
            "| Section | Rules F1 | LLM F1 |",
            "|---|---|---|",
        ]
        tpl = {m: [s for s in ev.scores[m] if s.source in ev.template] for m in ("rules", "llm")}
        for section in SECTIONS:
            r, m = total(tpl["rules"], section), total(tpl["llm"], section)
            lines.append(f"| {section} | {_f1_or_na(r)} | {_f1_or_na(m)} |")

    errors_method = "llm" if "llm" in ev.scores else main
    lines += ["", f"## Every {label[errors_method]} error", ""]
    any_errors = False
    for s in ev.scores[errors_method]:
        if s.errors:
            any_errors = True
            lines.append(f"**{s.source}**")
            lines += [f"- {e}" for e in s.errors] + [""]
    if not any_errors:
        lines.append("None.")

    if ev.usage:
        costs = [cost_usd(u["model"], u) for u in ev.usage]
        calls = len(ev.usage)
        tokens_in = sum(
            u.get("input_tokens", 0)
            + (u.get("cache_read_input_tokens") or 0)
            + (u.get("cache_creation_input_tokens") or 0)
            for u in ev.usage
        )
        tokens_out = sum(u.get("output_tokens", 0) for u in ev.usage)
        lines += ["", "## Cost", ""]
        lines.append(
            f"{calls} LLM extraction calls (one per gold report the LLM read), "
            f"{tokens_in:,} input tokens (including cached) and "
            f"{tokens_out:,} output tokens"
            + (
                f": ${sum(costs):.4f}, about ${sum(costs) / calls:.4f} per call."
                if None not in costs
                else "."
            )
        )
    return "\n".join(lines) + "\n"
