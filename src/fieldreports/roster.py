"""Project roster: canonical names for the project, superintendents, subs and trades.

Supers write "elec", "Volt Line", "volt line electric inc." and "sparkies" for the same company.
Man-hours by sub only works if all of those land on one name, so every extractor's output goes
through ``Roster.normalize`` before validation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from fieldreports.schema import ReportFields

DEFAULT_ROSTER = Path(__file__).resolve().parents[2] / "data" / "project.yaml"

_SUFFIXES = {"inc", "llc", "co", "corp", "company", "ltd"}


def name_key(text: str) -> str:
    """Lowercase, '&' -> 'and', drop punctuation and company suffixes, collapse spaces."""
    text = text.lower().replace("&", " and ").replace("_", " ").replace("-", " ")
    words = re.sub(r"[^a-z0-9 ]", " ", text).split()
    return " ".join(w for w in words if w not in _SUFFIXES)


@dataclass(frozen=True)
class Sub:
    company: str
    trade: str


class Roster:
    def __init__(self, data: dict):
        project = data["project"]
        self.project: str = project["name"]
        self.general_contractor: str = data.get("general_contractor", "")
        self._projects = {name_key(a) for a in [project["name"], *project.get("aliases", [])]}

        self.superintendents: list[str] = []
        self._supers: dict[str, str] = {}
        for sup in data.get("superintendents", []):
            self.superintendents.append(sup["name"])
            for alias in [sup["name"], *sup.get("aliases", [])]:
                self._supers[name_key(alias)] = sup["name"]

        self._trades: dict[str, str] = {}
        for trade, aliases in data.get("trades", {}).items():
            for alias in [trade, *aliases]:
                self._trades[name_key(alias)] = trade

        self.subs: list[Sub] = []
        self._companies: dict[str, Sub] = {}
        for entry in data.get("subs", []):
            sub = Sub(entry["company"], entry["trade"])
            self.subs.append(sub)
            for alias in [sub.company, *entry.get("aliases", [])]:
                self._companies[name_key(alias)] = sub

    @classmethod
    def load(cls, path: Path = DEFAULT_ROSTER) -> Roster:
        return cls(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    # -- lookups: each returns the canonical name, or None when unknown ------------------

    def canonical_project(self, name: str | None) -> str | None:
        if name and name_key(name) in self._projects:
            return self.project
        return None

    def canonical_super(self, name: str | None) -> str | None:
        return self._supers.get(name_key(name)) if name else None

    def canonical_trade(self, name: str) -> str | None:
        return self._trades.get(name_key(name))

    def match_company(self, name: str) -> Sub | None:
        """Exact alias match first; then a bare trade word if exactly one sub has that trade."""
        key = name_key(name)
        if key in self._companies:
            return self._companies[key]
        trade = self._trades.get(key)
        candidates = [s for s in self.subs if s.trade == trade]
        return candidates[0] if trade and len(candidates) == 1 else None

    def normalize(self, report: ReportFields) -> tuple[ReportFields, list[str]]:
        """Return a copy with canonical names, plus the names that could not be resolved.

        Unknown names are kept exactly as written (never dropped or guessed); the caller decides
        what an unknown company means for confidence.
        """
        unknown: list[str] = []
        out = report.model_copy(deep=True)

        if out.project is not None:
            canonical = self.canonical_project(out.project)
            if canonical:
                out.project = canonical
            else:
                unknown.append(f"project '{out.project}'")
        if out.superintendent is not None:
            out.superintendent = self.canonical_super(out.superintendent) or out.superintendent

        for crew in out.manpower:
            sub = self.match_company(crew.company)
            if sub:
                crew.company, crew.trade = sub.company, sub.trade
            else:
                unknown.append(f"company '{crew.company}'")
                crew.trade = self.canonical_trade(crew.trade) or crew.trade
        for item in out.work_completed:
            item.trade = self.canonical_trade(item.trade) or item.trade
        return out, unknown
