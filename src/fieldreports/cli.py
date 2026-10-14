"""Command-line interface. Commands are added milestone by milestone."""

import typer

from fieldreports import __version__

app = typer.Typer(help="Superintendent daily reports -> structured data.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Superintendent daily reports -> structured, queryable data."""


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)
