import json
from pathlib import Path

import pytest

from fieldreports.roster import Roster
from fieldreports.schema import ReportFields

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
GOLD = ROOT / "data" / "gold"


@pytest.fixture(scope="session")
def roster() -> Roster:
    return Roster.load()


def load_gold() -> list[tuple[str, ReportFields]]:
    """(source file name, expected report) for every gold file."""
    out = []
    for path in sorted(GOLD.glob("*.json")):
        gold = json.loads(path.read_text(encoding="utf-8"))
        out.append((gold["source_file"], ReportFields.model_validate(gold["report"])))
    return out
