from __future__ import annotations

import typer


def typer_factory(*, help: str) -> typer.Typer:
    return typer.Typer(
        help=help,
        no_args_is_help=True,
        add_completion=False,
        rich_markup_mode="markdown",
    )
