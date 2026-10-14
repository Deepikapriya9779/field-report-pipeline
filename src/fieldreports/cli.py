"""Command-line interface: ``fieldreports ingest | query | review``."""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from fieldreports import __version__, db, queries
from fieldreports.ingest import ingest_folder
from fieldreports.pipeline import Status
from fieldreports.roster import DEFAULT_ROSTER, Roster

load_dotenv()
app = typer.Typer(help="Superintendent daily reports -> structured data.", no_args_is_help=True)
query_app = typer.Typer(help="Ask the database questions.", no_args_is_help=True)
app.add_typer(query_app, name="query")
console = Console()

DbOption = Annotated[Path, typer.Option("--db", help="SQLite file.", envvar="FIELDREPORTS_DB")]
WeekOption = Annotated[
    datetime | None,
    typer.Option("--week", formats=["%Y-%m-%d"], help="Monday that starts the week."),
]


class ManhoursBy(StrEnum):
    sub = "sub"
    trade = "trade"
    day = "day"


class DelaysBy(StrEnum):
    cause = "cause"
    day = "day"


@app.callback()
def main() -> None:
    """Superintendent daily reports -> structured, queryable data."""


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


@app.command()
def ingest(
    folder: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    db_path: DbOption = Path("fieldreports.db"),
    roster_path: Annotated[Path, typer.Option("--roster")] = DEFAULT_ROSTER,
) -> None:
    """Load every report in FOLDER. Files already ingested are skipped."""
    conn = db.connect(db_path)
    result = ingest_folder(conn, folder, Roster.load(roster_path), today=date.today())

    table = Table("file", "status", "method", "conf", "reason")
    style = {Status.LOADED: "green", Status.REVIEW: "yellow", Status.PENDING: "dim"}
    for o in result.outcomes:
        method = o.report.extraction_method.value if o.report else ""
        conf = f"{o.report.confidence:.2f}" if o.report else ""
        reason = "; ".join(o.reasons)
        table.add_row(o.doc.path.name, f"[{style[o.status]}]{o.status}[/]", method, conf, reason)
    if result.outcomes:
        console.print(table)
    console.print(
        f"loaded {result.count(Status.LOADED)} | review {result.count(Status.REVIEW)} | "
        f"pending {result.count(Status.PENDING)} | already ingested {result.skipped}"
    )
    if result.count(Status.PENDING):
        console.print(
            "[dim]pending = not the DR-1 template; needs the LLM extractor, which is not "
            "built yet. Pending files are not recorded and will be picked up on a later run.[/]"
        )


def _print_rows(rows: list[dict], title: str, numeric_cols: tuple[str, ...] = ()) -> None:
    if not rows:
        console.print(f"{title}: no data")
        return
    table = Table(*rows[0].keys(), title=title)
    for row in rows:
        table.add_row(*(f"{v:,.1f}" if k in numeric_cols else str(v) for k, v in row.items()))
    console.print(table)


def _week(week: datetime | None) -> date | None:
    if week is None:
        return None
    try:
        queries.week_range(week.date())
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--week") from exc
    return week.date()


@query_app.command("manhours")
def query_manhours(
    by: Annotated[ManhoursBy, typer.Option("--by")] = ManhoursBy.sub,
    week: WeekOption = None,
    db_path: DbOption = Path("fieldreports.db"),
) -> None:
    """Man-hours (headcount x hours per worker) by sub, trade or day."""
    wk = _week(week)
    rows = queries.manhours(db.connect(db_path), by.value, wk)
    scope = f"week of {wk}" if wk else "all dates"
    _print_rows(rows, f"Man-hours by {by.value}, {scope}", ("man_hours",))


@query_app.command("delays")
def query_delays(
    by: Annotated[DelaysBy, typer.Option("--by")] = DelaysBy.cause,
    week: WeekOption = None,
    db_path: DbOption = Path("fieldreports.db"),
) -> None:
    """Reported delay hours by cause or day."""
    wk = _week(week)
    rows = queries.delays(db.connect(db_path), by.value, wk)
    scope = f"week of {wk}" if wk else "all dates"
    _print_rows(rows, f"Delay hours by {by.value}, {scope}", ("hours",))


@app.command()
def review(db_path: DbOption = Path("fieldreports.db")) -> None:
    """List reports waiting for a human, with the reason for each."""
    rows = queries.review_queue(db.connect(db_path))
    if not rows:
        console.print("review queue is empty")
        return
    table = Table("file", "reason", "queued (UTC)", title=f"Needs review ({len(rows)})")
    for row in rows:
        reasons = "\n".join(json.loads(row["reasons"]))
        table.add_row(os.path.basename(row["path"]), reasons, row["queued_at"])
    console.print(table)
