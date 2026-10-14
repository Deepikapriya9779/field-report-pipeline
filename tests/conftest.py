import json
import socket
from pathlib import Path

import pytest

from fieldreports.roster import Roster
from fieldreports.schema import ReportFields

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
GOLD = ROOT / "data" / "gold"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never hit the network: no API key, mock mode, and sockets refuse to connect."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("LLM_MODE", "mock")

    def refuse(*args, **kwargs):
        raise RuntimeError("tests must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


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
