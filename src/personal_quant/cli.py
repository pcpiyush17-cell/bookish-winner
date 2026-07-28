"""Command-line interface for explicit operator actions."""

from typing import Annotated

import typer

from personal_quant import __version__
from personal_quant.doctor import run_checks

app = typer.Typer(
    name="pq",
    help="Operate the Personal Quant Trading System.",
    no_args_is_help=True,
)


def version_callback(value: bool) -> None:
    """Print the package version and exit when requested."""
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=version_callback, is_eager=True, help="Show version."),
    ] = None,
) -> None:
    """Operate the Personal Quant Trading System."""


@app.command()
def doctor() -> None:
    """Validate the local runtime without contacting external services."""
    checks = run_checks()
    for check in checks:
        marker = "PASS" if check.passed else "FAIL"
        typer.echo(f"[{marker}] {check.name}: {check.detail}")

    if not all(check.passed for check in checks):
        raise typer.Exit(code=1)

    typer.echo("Environment is ready for local development.")
