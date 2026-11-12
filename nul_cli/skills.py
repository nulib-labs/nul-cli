from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import _skills
from ._cli_utils import typer_factory
from .errors import CLIError

CENTRAL_LOCAL = Path(".agents/skills")
CENTRAL_GLOBAL = Path("~/.agents/skills")
CLAUDE_LOCAL = Path(".claude/skills")
CLAUDE_GLOBAL = Path("~/.claude/skills")

skills_cli = typer_factory(help="Manage skills for AI assistants.")
console = Console()


def _remove_existing(path: Path, force: bool) -> None:
    if not (path.exists() or path.is_symlink()):
        return
    if not force:
        raise CLIError(f"Skill already exists at {path}.\nRe-run with --force to overwrite.")
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _create_symlink(agent_skills_dir: Path, central_skill_path: Path, force: bool) -> Path:
    agent_skills_dir = agent_skills_dir.expanduser().resolve()
    agent_skills_dir.mkdir(parents=True, exist_ok=True)
    link_path = agent_skills_dir / central_skill_path.name
    _remove_existing(link_path, force)
    link_path.symlink_to(os.path.relpath(central_skill_path, agent_skills_dir))
    return link_path


def _resolve_update_roots(*, claude: bool, global_: bool, dest: Path | None) -> list[Path]:
    if dest is not None:
        if claude or global_:
            raise CLIError("--dest cannot be combined with --claude or --global.")
        return [dest.expanduser().resolve()]
    roots: list[Path] = [CENTRAL_GLOBAL if global_ else CENTRAL_LOCAL]
    if claude:
        roots.append(CLAUDE_GLOBAL if global_ else CLAUDE_LOCAL)
    return [r.expanduser().resolve() for r in roots]


def _install_to(skills_dir: Path, name: str, force: bool) -> list[Path]:
    try:
        return _skills.install_skill(name, skills_dir, force=force)
    except FileExistsError as exc:
        raise CLIError(f"{exc}\nRe-run with --force to overwrite.") from exc


@skills_cli.command("preview")
def skills_preview(
    name: Annotated[
        str,
        typer.Argument(
            help="Skill selector: `<plugin>` or `<plugin>:<skill>`.", show_default=False
        ),
    ] = _skills.DEFAULT_SKILL_ID,
) -> None:
    """Print a skill's SKILL.md to stdout."""
    print(_skills.preview_skill(name))


@skills_cli.command("list")
def skills_list() -> None:
    """List available plugins (each plugin may install one or more skills)."""
    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("Plugin", no_wrap=True)
    table.add_column("Origin", no_wrap=True)
    table.add_column("Description")
    for s in _skills.list_all_plugins():
        table.add_row(s.name, s.origin, s.description)
    console.print(table)


@skills_cli.command("add")
def skills_add(
    name: Annotated[
        str,
        typer.Argument(
            help="Plugin name, optionally `<plugin>:<skill>` to install one skill from it.",
            show_default=False,
        ),
    ] = _skills.DEFAULT_SKILL_ID,
    claude: Annotated[bool, typer.Option("--claude", help="Install for Claude.")] = False,
    global_: Annotated[
        bool,
        typer.Option(
            "--global",
            "-g",
            help="Install globally (user-level) instead of in the current project directory.",
        ),
    ] = False,
    dest: Annotated[
        Path | None,
        typer.Option(help="Install into a custom destination (path to skills directory)."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite existing skills in the destination.")
    ] = False,
) -> None:
    """Install a skill for an AI assistant.

    Default location is the current directory (`.agents/skills`) or user-level
    (`~/.agents/skills`). With `--claude`, installed skills are also symlinked into
    Claude's skills directory. Plugins with multiple skills install each one
    as a separate `.agents/skills/<plugin>-<skill>/` directory unless you scope
    with `<plugin>:<skill>`.

    Examples:
      - nul skills add
      - nul skills add meadow
      - nul skills add meadow:run-tests --dest=~/my-skills
      - nul skills add --global
      - nul skills add meadow --claude --global
    """
    if dest is not None:
        if claude or global_:
            raise CLIError("--dest cannot be combined with --claude or --global.")
        installed = _install_to(dest, name, force)
        for path in installed:
            print(f"Installed '{path.name}' to {path}")
        return

    central_path = CENTRAL_GLOBAL if global_ else CENTRAL_LOCAL
    installed = _install_to(central_path, name, force)
    for path in installed:
        print(f"Installed '{path.name}' to central location: {path}")

    if claude:
        agent_target = CLAUDE_GLOBAL if global_ else CLAUDE_LOCAL
        for path in installed:
            link_path = _create_symlink(agent_target, path, force)
            print(f"Created symlink: {link_path}")


@skills_cli.command("upgrade")
def skills_upgrade(
    name: Annotated[
        str | None,
        typer.Argument(
            help="Optional installed skill directory name to upgrade.", show_default=False
        ),
    ] = None,
    claude: Annotated[
        bool, typer.Option("--claude", help="Upgrade skills installed for Claude.")
    ] = False,
    global_: Annotated[
        bool,
        typer.Option(
            "--global", "-g", help="Use global skills directories instead of the current project."
        ),
    ] = False,
    dest: Annotated[
        Path | None, typer.Option(help="Upgrade skills in a custom skills directory.")
    ] = None,
) -> None:
    """Upgrade installed skills.

    For each managed skill, the CLI compares the installed revision against the
    upstream commit SHA at the plugin's path (or the local content hash, when
    iterating via `NUL_SKILLS_PATH`). Skills with a different revision are
    reinstalled in place.

    Examples:
      - nul skills upgrade
      - nul skills upgrade nul-cli
      - nul skills upgrade meadow-run-tests --dest=~/my-skills
      - nul skills upgrade --claude
    """
    roots = _resolve_update_roots(claude=claude, global_=global_, dest=dest)
    results = _skills.apply_updates(roots, selector=name)
    if not results:
        print("No installed skills found.")
        return
    for r in results:
        detail = f" ({r.detail})" if r.detail else ""
        print(f"{r.name}: {r.status}{detail}")
