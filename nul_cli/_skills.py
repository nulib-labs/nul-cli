from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from tqdm import tqdm
from typer import Context
from typer.core import TyperCommand as Command
from typer.core import TyperGroup as Group
from typer.main import get_command

from .errors import CLIError
from .types import model

DEFAULT_SKILLS_REPO = "nulib-labs/nul-skills"
DEFAULT_SKILLS_REF = "main"
MARKETPLACE_PATH = ".claude-plugin/marketplace.json"
PLUGIN_MANIFEST_PATH = ".claude-plugin/plugin.json"
PLUGIN_SKILLS_DIR = "skills"
MANIFEST_FILE = ".nul-skill-manifest.json"
MANIFEST_SCHEMA_VERSION = 2
NUL_CLI_SKILL_ID = "nul-cli"
DEFAULT_SKILL_ID = NUL_CLI_SKILL_ID
LOCAL_REVISION_PREFIX = "local:"


@model(frozen=True)
class CommunityPlugin:
    name: str
    source: str
    description: str = ""


@model(frozen=True)
class InstalledSkill:
    schema_version: int
    plugin: str
    name: str
    repo: str
    source: str
    revision: str


@model(frozen=True)
class SkillSummary:
    name: str
    description: str
    origin: str  # "generated" | "community"


@model(frozen=True)
class SkillUpdateInfo:
    name: str
    status: str
    detail: str | None = None


# --- env / source resolution ---


def _skills_repo() -> str:
    return os.environ.get("NUL_SKILLS_REPO", DEFAULT_SKILLS_REPO)


def _skills_ref() -> str:
    return os.environ.get("NUL_SKILLS_REF", DEFAULT_SKILLS_REF)


def _explicit_local_path() -> Path | None:
    raw = os.environ.get("NUL_SKILLS_PATH")
    if not raw:
        return None
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise CLIError(f"NUL_SKILLS_PATH points at a non-existent directory: {path}")
    return path


def _local_root() -> Path | None:
    return _explicit_local_path()


# --- HTTP helpers ---


def _http_get(url: str, *, accept: str | None = None) -> bytes:
    req = urllib.request.Request(url)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if accept:
        req.add_header("Accept", accept)
    req.add_header("User-Agent", "nul-cli")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise CLIError(f"GitHub request failed ({exc.code} {exc.reason}): {url}") from exc
    except urllib.error.URLError as exc:
        raise CLIError(f"Network error fetching {url}: {exc.reason}") from exc


def _api_url(api_path: str, **params: str) -> str:
    base = f"https://api.github.com/repos/{_skills_repo()}/{api_path.lstrip('/')}"
    if params:
        base += "?" + urllib.parse.urlencode(params)
    return base


# --- catalog (Claude Code plugin marketplace manifest) ---
# The catalog file lives at `.claude-plugin/marketplace.json` and uses a
# `plugins` list. Both names follow the Claude Code plugin marketplace
# convention so the same repo can also be added as a Claude plugin source —
# we keep them verbatim despite our user-facing "community" terminology.


def _read_catalog() -> dict:
    local = _local_root()
    if local is not None:
        data = json.loads((local / MARKETPLACE_PATH).read_text(encoding="utf-8"))
    else:
        body = _http_get(
            _api_url(f"contents/{MARKETPLACE_PATH}", ref=_skills_ref()),
            accept="application/vnd.github+json",
        )
        payload = json.loads(body)
        if payload.get("encoding") != "base64":
            raise CLIError(f"Unexpected catalog encoding: {payload.get('encoding')!r}")
        data = json.loads(base64.b64decode(payload["content"]))

    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        raise CLIError("NU community catalog is missing a 'plugins' list.")
    return data


def list_community_plugins() -> list[CommunityPlugin]:
    plugins: list[CommunityPlugin] = []
    for entry in _read_catalog()["plugins"]:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        source = entry.get("source")
        description = entry.get("description") or ""
        if isinstance(name, str) and isinstance(source, str):
            plugins.append(CommunityPlugin(name=name, source=source, description=str(description)))
    return sorted(plugins, key=lambda p: p.name)


def find_community_plugin(name: str) -> CommunityPlugin:
    available = list_community_plugins()
    for plugin in available:
        if plugin.name == name:
            return plugin
    names = ", ".join(p.name for p in available) or "(none)"
    raise CLIError(f"Unknown NU community plugin '{name}'. Available: {names}")


# --- revision resolution ---


def _normalize_source_path(source: str) -> str:
    return source.lstrip("./").rstrip("/")


def _iter_files(root: Path) -> Iterable[tuple[str, Path]]:
    for path in root.rglob("*"):
        if path.is_file():
            yield str(path.relative_to(root)).replace(os.sep, "/"), path


def _hash_dir(root: Path) -> str:
    h = hashlib.sha256()
    for relpath, file_path in sorted(_iter_files(root)):
        h.update(relpath.encode("utf-8"))
        h.update(b"\0")
        h.update(file_path.read_bytes())
        h.update(b"\0")
    return f"{LOCAL_REVISION_PREFIX}{h.hexdigest()[:16]}"


def _remote_revision(source_path: str) -> str:
    body = _http_get(
        _api_url("commits", path=source_path, sha=_skills_ref(), per_page="1"),
        accept="application/vnd.github+json",
    )
    commits = json.loads(body)
    if not isinstance(commits, list) or not commits:
        raise CLIError(f"No commits found for {source_path} on {_skills_ref()}.")
    sha = commits[0].get("sha")
    if not isinstance(sha, str):
        raise CLIError("GitHub commit response missing sha.")
    return sha


def resolve_plugin_revision(plugin: CommunityPlugin) -> str:
    source_path = _normalize_source_path(plugin.source)
    local = _local_root()
    if local is not None:
        return _hash_dir(local / source_path)
    return _remote_revision(source_path)


# --- plugin file fetching ---


def _read_plugin_files_local(plugin_dir: Path) -> dict[str, bytes]:
    if not plugin_dir.is_dir():
        raise CLIError(f"Plugin directory missing: {plugin_dir}")
    return {rel: file_path.read_bytes() for rel, file_path in _iter_files(plugin_dir)}


def _read_plugin_files_tarball(source_path: str, revision: str) -> dict[str, bytes]:
    url = f"https://codeload.github.com/{_skills_repo()}/tar.gz/{revision}"
    target_prefix = source_path.rstrip("/") + "/"
    files: dict[str, bytes] = {}
    try:
        with urllib.request.urlopen(url) as resp:
            with tarfile.open(fileobj=resp, mode="r|gz") as tar:
                for member in tar:
                    if not member.isfile():
                        continue
                    parts = member.name.split("/", 1)
                    if len(parts) < 2:
                        continue
                    inner = parts[1]
                    if not inner.startswith(target_prefix):
                        continue
                    rel = inner[len(target_prefix) :]
                    if not rel:
                        continue
                    handle = tar.extractfile(member)
                    if handle is None:
                        continue
                    files[rel] = handle.read()
    except urllib.error.URLError as exc:
        raise CLIError(f"Failed to fetch tarball at {url}: {exc.reason}") from exc
    except tarfile.TarError as exc:
        raise CLIError(f"Failed to read tarball at {url}: {exc}") from exc
    return files


def _read_plugin_files(plugin: CommunityPlugin, revision: str) -> dict[str, bytes]:
    source_path = _normalize_source_path(plugin.source)
    local = _local_root()
    if local is not None:
        return _read_plugin_files_local(local / source_path)
    return _read_plugin_files_tarball(source_path, revision)


def _validate_plugin_manifest(plugin: CommunityPlugin, files: dict[str, bytes]) -> None:
    raw = files.get(PLUGIN_MANIFEST_PATH)
    if raw is None:
        raise CLIError(
            f"Plugin '{plugin.name}' is missing {PLUGIN_MANIFEST_PATH}. "
            "Catalog entries must point at a Claude Code plugin directory."
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CLIError(f"Plugin '{plugin.name}' has invalid plugin.json: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("name"), str):
        raise CLIError(f"Plugin '{plugin.name}' plugin.json is missing a 'name' field.")


def _list_skill_names(files: dict[str, bytes]) -> list[str]:
    prefix = f"{PLUGIN_SKILLS_DIR}/"
    skill_md_suffix = "/SKILL.md"
    names: set[str] = set()
    for rel in files:
        if not rel.startswith(prefix) or not rel.endswith(skill_md_suffix):
            continue
        inner = rel[len(prefix) :]
        skill_name, _, tail = inner.partition("/")
        if not skill_name or tail != "SKILL.md":
            continue
        names.add(skill_name)
    return sorted(names)


def _extract_skill_files(files: dict[str, bytes], skill_name: str) -> dict[str, bytes]:
    prefix = f"{PLUGIN_SKILLS_DIR}/{skill_name}/"
    return {rel[len(prefix) :]: content for rel, content in files.items() if rel.startswith(prefix)}


# --- install ---


def _installed_skill_dir_name(plugin_name: str, skill_name: str) -> str:
    return f"{plugin_name}-{skill_name}"


def _manifest_path(skill_path: Path) -> Path:
    return skill_path / MANIFEST_FILE


def _atomic_replace(staging: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    shutil.move(str(staging), str(target))


def _stage_skill(
    skills_dir: Path,
    name: str,
    files: dict[str, bytes],
    manifest: InstalledSkill,
) -> Path:
    skills_dir.mkdir(parents=True, exist_ok=True)
    staging = skills_dir / f".staging-{name}-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        for rel, content in files.items():
            rel_path = Path(rel)
            if rel_path.is_absolute() or ".." in rel_path.parts:
                raise CLIError(f"Refusing to install unsafe path: {rel}")
            file_path = staging / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(content)
        _manifest_path(staging).write_text(json.dumps(asdict(manifest), indent=2) + "\n", encoding="utf-8")
        target = skills_dir / name
        _atomic_replace(staging, target)
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _install_one_skill(
    *,
    plugin: CommunityPlugin,
    skill_name: str,
    plugin_files: dict[str, bytes],
    revision: str,
    skills_dir: Path,
    force: bool,
) -> Path:
    installed_name = _installed_skill_dir_name(plugin.name, skill_name)
    target = skills_dir / installed_name
    if (target.exists() or target.is_symlink()) and not force:
        raise FileExistsError(f"Skill already exists at {target}")

    skill_files = _extract_skill_files(plugin_files, skill_name)
    if "SKILL.md" not in skill_files:
        raise CLIError(
            f"Plugin '{plugin.name}' skill '{skill_name}' is missing SKILL.md (revision {revision})."
        )
    manifest = InstalledSkill(
        schema_version=MANIFEST_SCHEMA_VERSION,
        plugin=plugin.name,
        name=skill_name,
        repo=_skills_repo(),
        source=_normalize_source_path(plugin.source),
        revision=revision,
    )
    return _stage_skill(skills_dir, installed_name, skill_files, manifest)


def install_community_plugin(
    plugin_name: str,
    skills_dir: Path,
    *,
    skill: str | None = None,
    force: bool = False,
) -> list[Path]:
    """Install one or all skills from a community plugin.

    Returns the list of installed skill directories. Pass ``skill`` to install
    a single skill from the plugin; otherwise every skill in the plugin is
    installed.
    """
    skills_dir = skills_dir.expanduser().resolve()
    plugin = find_community_plugin(plugin_name)
    revision = resolve_plugin_revision(plugin)

    files: dict[str, bytes] = {}
    installed: list[Path] = []
    steps = ["Resolve revision", "Download plugin", "Install skills"]
    with tqdm(steps, desc=f"Installing {plugin_name}", unit="step", leave=False) as progress:
        for step in progress:
            progress.set_postfix_str(step)
            if step == "Download plugin":
                files = _read_plugin_files(plugin, revision)
                _validate_plugin_manifest(plugin, files)
            elif step == "Install skills":
                available = _list_skill_names(files)
                if not available:
                    raise CLIError(f"Plugin '{plugin_name}' contains no skills.")
                if skill is not None:
                    if skill not in available:
                        names = ", ".join(available)
                        raise CLIError(
                            f"Plugin '{plugin_name}' has no skill '{skill}'. Available: {names}"
                        )
                    targets = [skill]
                else:
                    targets = available
                for skill_name in targets:
                    installed.append(
                        _install_one_skill(
                            plugin=plugin,
                            skill_name=skill_name,
                            plugin_files=files,
                            revision=revision,
                            skills_dir=skills_dir,
                            force=force,
                        )
                    )

    return installed


# --- dynamic nul-cli skill (generated locally) ---

_NUL_CLI_DESCRIPTION = (
    "Northwestern University CLI (`nul`) command reference generated from your local installation. "
    "Use when running NU CLI workflows, discovering command usage, and selecting the right options for "
    "project-level and user-level skill installation/upgrade tasks."
)

_SKILL_YAML_PREFIX = f"""\
---
name: nul-cli
description: "{_NUL_CLI_DESCRIPTION}"
---

The NU CLI tool `nul` is available.

Use `nul --help` to view available command groups. Use `nul <group> --help` and `nul <group> <command> --help` for details.
"""

_SKILL_TIPS = """
## Tips

- Use `nul <command> --help` for full options, descriptions, and examples.
- Run `nul skills add --force` to regenerate and reinstall the latest generated `nul-cli` skill.
"""

_COMMON_FLAG_ALLOWLIST = {"--claude", "--global", "--force", "--dest"}
_INLINE_FLAG_EXCLUDE: set[str] = set()
_COMMON_FLAG_HELP_OVERRIDES: dict[str, str] = {
    "--global": "Install into user-level location (`~/.agents/skills`) instead of project-local.",
    "--claude": "Also wire skills into Claude skills directory (`.claude/skills` or `~/.claude/skills`).",
    "--dest": "Install or upgrade skills in a custom destination directory.",
    "--force": "Overwrite existing installed skill content in the target location.",
}


def _format_params(cmd: Command) -> str:
    parts: list[str] = []
    for p in cmd.params:
        if not p.required or p.name == "help":
            continue
        if p.name and p.name.startswith("_"):
            continue
        long_name = next((o for o in getattr(p, "opts", []) if o.startswith("--")), None)
        if long_name is not None:
            type_name = p.bare_type().upper() or "VALUE"
            parts.append(f"{long_name} {type_name}")
        elif p.name:
            parts.append(p.display_name_raw)
    return " ".join(parts)


def _collect_leaf_commands(group: Group, ctx: Context, path_parts: list[str]) -> list[tuple[list[str], Command]]:
    leaves: list[tuple[list[str], Command]] = []
    sub_ctx = Context(group, parent=ctx, info_name=path_parts[-1])
    for name in group.list_commands(sub_ctx):
        cmd = group.get_command(sub_ctx, name)
        if cmd is None or cmd.hidden:
            continue
        child_path = [*path_parts, name]
        if isinstance(cmd, Group):
            leaves.extend(_collect_leaf_commands(cmd, sub_ctx, child_path))
        else:
            leaves.append((child_path, cmd))
    return leaves


def _iter_optional_params(cmd: Command):
    for p in cmd.params:
        if p.required or p.name == "help":
            continue
        if p.name and p.name.startswith("_"):
            continue
        long_name = None
        short_name = None
        for opt in getattr(p, "opts", []):
            if opt.startswith("--"):
                long_name = long_name or opt
            elif opt.startswith("-"):
                short_name = opt
        if long_name:
            yield p, long_name, short_name


def _get_flag_names(cmd: Command, *, exclude: set[str] | None = None) -> list[str]:
    flags: list[str] = []
    for p, long_name, _short in _iter_optional_params(cmd):
        if exclude and long_name in exclude:
            continue
        if getattr(p, "is_flag", False):
            flags.append(long_name)
        else:
            type_name = p.bare_type().upper() or "VALUE"
            flags.append(f"{long_name} {type_name}")
    return flags


def _compute_common_flags(leaf_commands: list[tuple[list[str], Command]]) -> dict[str, tuple[str, str]]:
    flag_info: dict[str, tuple[str, str]] = {}
    for _path, cmd in leaf_commands:
        for p, long_name, short_name in _iter_optional_params(cmd):
            if long_name not in _COMMON_FLAG_ALLOWLIST:
                continue
            if long_name not in flag_info or (short_name and " / " not in flag_info[long_name][0]):
                display = f"{short_name} / {long_name}" if short_name else long_name
                help_text = (getattr(p, "help", None) or "").split("\n")[0].strip()
                flag_info[long_name] = (display, help_text)
    return flag_info


def _render_leaf(cli_name: str, path_parts: list[str], cmd: Command) -> str:
    help_text = (cmd.help or "").split("\n")[0].strip()
    params = _format_params(cmd)
    parts = [cli_name, *path_parts] + ([params] if params else [])
    entry = f"- `{' '.join(parts)}`"
    if help_text:
        entry += f" — {help_text}"
    flags = _get_flag_names(cmd, exclude=_INLINE_FLAG_EXCLUDE)
    if flags:
        entry += f" `[{', '.join(flags)}]`"
    return entry


def build_skill_md() -> str:
    from . import __version__
    from .main import app

    click_app = get_command(app)
    cli_name = "nul"
    ctx = Context(click_app, info_name=cli_name)

    top_level: list[tuple[list[str], Command]] = []
    groups: list[tuple[str, Group]] = []
    for name in sorted(click_app.list_commands(ctx)):
        cmd = click_app.get_command(ctx, name)
        if cmd is None or cmd.hidden:
            continue
        if isinstance(cmd, Group):
            groups.append((name, cmd))
        else:
            top_level.append(([name], cmd))

    group_leaves: list[tuple[str, list[tuple[list[str], Command]]]] = []
    all_leaf_commands: list[tuple[list[str], Command]] = list(top_level)
    for name, group in groups:
        leaves = _collect_leaf_commands(group, ctx, [name])
        group_leaves.append((name, leaves))
        all_leaf_commands.extend(leaves)

    common_flags = _compute_common_flags(all_leaf_commands)

    lines: list[str] = list(_SKILL_YAML_PREFIX.splitlines())
    lines.append("")
    lines.append(f"Generated with `nul-cli v{__version__}`. Run `nul skills add --force` to regenerate.")
    lines.append("")
    lines.append("## Commands")
    lines.append("")
    for path_parts, cmd in top_level:
        lines.append(_render_leaf(cli_name, path_parts, cmd))

    groups_dict = dict(groups)
    for name, leaves in group_leaves:
        group_cmd = groups_dict[name]
        help_text = (group_cmd.help or "").split("\n")[0].strip()
        lines.append("")
        if help_text:
            lines.append(f"### `{cli_name} {name}` — {help_text}")
        else:
            lines.append(f"### `{cli_name} {name}`")
        lines.append("")
        for path_parts, cmd in leaves:
            lines.append(_render_leaf(cli_name, path_parts, cmd))

    if common_flags:
        lines.append("")
        lines.append("## Common options")
        lines.append("")
        for long_name, (display, help_text) in sorted(common_flags.items()):
            display_help = _COMMON_FLAG_HELP_OVERRIDES.get(long_name, help_text)
            if display_help:
                lines.append(f"- `{display}` — {display_help}")
            else:
                lines.append(f"- `{display}`")

    lines.extend(_SKILL_TIPS.splitlines())

    return "\n".join(lines)


def _nul_cli_revision() -> str:
    from . import __version__

    return f"nul-cli@{__version__}"


def install_nul_cli_skill(skills_dir: Path, *, force: bool = False) -> Path:
    skills_dir = skills_dir.expanduser().resolve()
    target = skills_dir / NUL_CLI_SKILL_ID
    if (target.exists() or target.is_symlink()) and not force:
        raise FileExistsError(f"Skill already exists at {target}")

    files = {"SKILL.md": build_skill_md().encode("utf-8")}
    manifest = InstalledSkill(
        schema_version=MANIFEST_SCHEMA_VERSION,
        plugin=NUL_CLI_SKILL_ID,
        name=NUL_CLI_SKILL_ID,
        repo="generated:nul-cli",
        source="generated:nul-cli",
        revision=_nul_cli_revision(),
    )
    return _stage_skill(skills_dir, NUL_CLI_SKILL_ID, files, manifest)


# --- public listing / preview / install dispatch ---


def list_all_plugins() -> list[SkillSummary]:
    summaries = [
        SkillSummary(
            name=NUL_CLI_SKILL_ID,
            description="NU CLI command reference generated from your installed `nul` command tree.",
            origin="generated",
        )
    ]
    summaries.extend(
        SkillSummary(name=p.name, description=p.description, origin="community")
        for p in list_community_plugins()
    )
    return summaries


def _parse_skill_selector(name: str) -> tuple[str, str | None]:
    """Split a `<plugin>` or `<plugin>:<skill>` selector."""
    plugin, sep, skill = name.partition(":")
    if sep:
        return plugin, (skill or None)
    return plugin, None


def preview_skill(name: str) -> str:
    if name == NUL_CLI_SKILL_ID:
        return build_skill_md()

    plugin_name, skill_name = _parse_skill_selector(name)
    plugin = find_community_plugin(plugin_name)
    revision = resolve_plugin_revision(plugin)
    files = _read_plugin_files(plugin, revision)
    _validate_plugin_manifest(plugin, files)

    available = _list_skill_names(files)
    if not available:
        raise CLIError(f"Plugin '{plugin_name}' contains no skills.")
    if skill_name is None:
        if len(available) == 1:
            skill_name = available[0]
        else:
            names = ", ".join(available)
            raise CLIError(
                f"Plugin '{plugin_name}' has multiple skills ({names}). "
                f"Use 'nul skills preview {plugin_name}:<skill>'."
            )
    if skill_name not in available:
        names = ", ".join(available)
        raise CLIError(f"Plugin '{plugin_name}' has no skill '{skill_name}'. Available: {names}")

    skill_files = _extract_skill_files(files, skill_name)
    md = skill_files.get("SKILL.md")
    if md is None:
        raise CLIError(f"Skill '{skill_name}' has no SKILL.md.")
    return md.decode("utf-8")


def install_skill(name: str, skills_dir: Path, *, force: bool = False) -> list[Path]:
    if name == NUL_CLI_SKILL_ID:
        return [install_nul_cli_skill(skills_dir, force=force)]

    plugin_name, skill_name = _parse_skill_selector(name)
    return install_community_plugin(plugin_name, skills_dir, skill=skill_name, force=force)


# --- upgrade ---


def _read_manifest(skill_path: Path) -> InstalledSkill | None:
    manifest_file = _manifest_path(skill_path)
    if not manifest_file.is_file():
        return None
    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    try:
        return InstalledSkill(
            schema_version=int(data.get("schema_version", 0)),
            plugin=str(data.get("plugin", data.get("name", ""))),
            name=str(data["name"]),
            repo=str(data.get("repo", "")),
            source=str(data.get("source", "")),
            revision=str(data["revision"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _iter_managed_skills(root: Path, selector: str | None) -> list[tuple[Path, InstalledSkill]]:
    root = root.expanduser().resolve()
    if not root.exists() or not root.is_dir():
        return []
    out: list[tuple[Path, InstalledSkill]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if selector is not None and entry.name != selector:
            continue
        manifest = _read_manifest(entry)
        if manifest is None:
            continue
        out.append((entry, manifest))
    return out


def _check_and_upgrade(skill_path: Path, manifest: InstalledSkill) -> SkillUpdateInfo:
    if manifest.plugin == NUL_CLI_SKILL_ID:
        new_revision = _nul_cli_revision()
        if new_revision == manifest.revision:
            return SkillUpdateInfo(name=manifest.name, status="up-to-date", detail=manifest.revision)
        install_nul_cli_skill(skill_path.parent, force=True)
        return SkillUpdateInfo(
            name=manifest.name,
            status="upgraded",
            detail=f"{manifest.revision} -> {new_revision}",
        )

    plugin = find_community_plugin(manifest.plugin)
    new_revision = resolve_plugin_revision(plugin)
    if new_revision == manifest.revision:
        return SkillUpdateInfo(name=manifest.name, status="up-to-date", detail=manifest.revision)
    install_community_plugin(
        manifest.plugin, skill_path.parent, skill=manifest.name, force=True
    )
    return SkillUpdateInfo(
        name=manifest.name,
        status="upgraded",
        detail=f"{manifest.revision} -> {new_revision}",
    )


def apply_updates(roots: list[Path], *, selector: str | None = None) -> list[SkillUpdateInfo]:
    seen: set[Path] = set()
    targets: list[tuple[Path, InstalledSkill]] = []
    for root in roots:
        for skill_path, manifest in _iter_managed_skills(root, selector):
            resolved = skill_path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            targets.append((skill_path, manifest))

    results: list[SkillUpdateInfo] = []
    with tqdm(targets, desc="Checking skills", unit="skill", leave=False) as progress:
        for skill_path, manifest in progress:
            try:
                results.append(_check_and_upgrade(skill_path, manifest))
            except CLIError as exc:
                results.append(SkillUpdateInfo(name=manifest.name, status="error", detail=str(exc)))
    return results
