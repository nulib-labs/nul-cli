from __future__ import annotations

import typer

from ._cli_utils import typer_factory
from .errors import CLIError
from .skills import skills_cli

app = typer_factory(help="NU command line interface.")
app.add_typer(skills_cli, name="skills")


def run() -> None:
    try:
        app()
    except CLIError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    run()
