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

from fieldreports import __version__, db, llm, queries
from fieldreports.ingest import ingest_folder
from fieldreports.pipeline import Status
from fieldreports.roster import DEFAULT_ROSTER, Roster
from fieldreports.weekly import build_weekly

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
    rules_only: Annotated[
        bool, typer.Option("--rules-only", help="Skip the LLM; free-form reports stay pending.")
    ] = False,
) -> None:
    """Load every report in FOLDER. Files already ingested are skipped.

    LLM mode comes from LLM_MODE (mock | record | live); without an API key it is mock,
    which replays recorded responses and fails if one is missing.
    """
    roster = Roster.load(roster_path)
    extractor = None
    if not rules_only:
        try:
            mode = llm.resolve_mode()
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="LLM_MODE") from exc
        extractor = llm.build_extractor(roster, mode)
        console.print(f"[dim]LLM: {extractor.model}, mode={mode}[/]")

    conn = db.connect(db_path)
    try:
        result = ingest_folder(conn, folder, roster, today=date.today(), llm=extractor)
    except llm.FixtureError as exc:
        console.print(f"[red]Stopped:[/] {exc}")
        console.print("[dim]Files processed before this one are saved; re-running is safe.[/]")
        raise typer.Exit(1) from exc

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
            "[dim]pending = not the DR-1 template and the LLM was skipped (--rules-only). "
            "Pending files are not recorded and will be picked up on a later run.[/]"
        )
    if extractor and extractor.usage:
        calls = extractor.usage
        tokens_in = sum(u.get("input_tokens", 0) for u in calls)
        tokens_out = sum(u.get("output_tokens", 0) for u in calls)
        costs = [llm.cost_usd(u["model"], u) for u in calls]
        cost = f", ${sum(costs):.4f}" if None not in costs else ""
        note = " (replayed from fixtures, nothing billed)" if mode is llm.LLMMode.MOCK else ""
        console.print(
            f"[dim]LLM calls {len(calls)}: {tokens_in:,} input + {tokens_out:,} output "
            f"tokens{cost}{note}[/]"
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


@app.command()
def weekly(
    week: Annotated[
        datetime, typer.Option("--week", formats=["%Y-%m-%d"], help="Monday that starts the week.")
    ],
    out: Annotated[Path | None, typer.Option("--out", help="Markdown file to write.")] = None,
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Use the template summary.")] = False,
    db_path: DbOption = Path("fieldreports.db"),
    roster_path: Annotated[Path, typer.Option("--roster")] = DEFAULT_ROSTER,
) -> None:
    """Write the weekly summary: tables from SQL, one LLM-written paragraph on top."""
    wk = _week(week)
    narrator = None
    if not no_llm:
        try:
            narrator = llm.build_narrator(llm.resolve_mode())
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="LLM_MODE") from exc
    try:
        markdown, source = build_weekly(
            db.connect(db_path), wk, Roster.load(roster_path).project, narrator
        )
    except llm.FixtureError as exc:
        console.print(f"[red]Stopped:[/] {exc}  (or use --no-llm for the template summary)")
        raise typer.Exit(1) from exc
    target = out or Path(f"weekly_{wk.isoformat()}.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8", newline="\n")
    console.print(f"wrote {target} (summary paragraph: {source})")
