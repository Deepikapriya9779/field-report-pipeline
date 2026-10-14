"""Turn a report file (.txt, .eml, .pdf) into plain text, and fingerprint it.

The sha256 is taken over the raw file bytes, so re-ingesting the same file is a no-op no matter
how it was renamed or moved.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pdfplumber

SUPPORTED = (".txt", ".eml", ".pdf")


@dataclass(frozen=True)
class SourceDoc:
    path: Path
    sha256: str
    text: str


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def discover(folder: Path) -> list[Path]:
    """Supported files in a folder, sorted by name so runs are reproducible."""
    return sorted(p for p in Path(folder).iterdir() if p.suffix.lower() in SUPPORTED)


def load(path: Path) -> SourceDoc:
    path = Path(path)
    return SourceDoc(path=path, sha256=file_sha256(path), text=load_text(path))


def load_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".txt":
        text = path.read_bytes().decode("utf-8", errors="replace")
    elif suffix == ".eml":
        text = _email_text(path.read_bytes())
    elif suffix == ".pdf":
        text = _pdf_text(path)
    else:
        raise ValueError(f"unsupported file type: {path.name}")
    return text.replace("\r\n", "\n").strip() + "\n"


def _email_text(raw: bytes) -> str:
    """Sender, recipient and subject, then the plain-text body.

    The ``Date:`` header is dropped on purpose: a report emailed after midnight carries the
    next day's date, and the report date must come from what the super wrote.
    """
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    body_part = msg.get_body(preferencelist=("plain",))
    body = body_part.get_content() if body_part is not None else ""
    headers = [f"{name}: {msg[name]}" for name in ("From", "To", "Subject") if msg[name]]
    return "\n".join(headers) + "\n\n" + body


def _pdf_text(path: Path) -> str:
    """Text layer only. A scanned PDF with no text layer comes back empty (no OCR here)."""
    with pdfplumber.open(path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)
