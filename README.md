# nul-cli

Northwestern University Libraries command-line tooling.

`nul` installs AI-assistant skills for Northwestern University Libraries workflows. It reads the
[NUL community catalog](https://github.com/nulib-labs/nul-skills) — a Claude
Code plugin marketplace — and writes each plugin's SKILL.md files into the
agent skill directories used by Codex, ChatGPT, Cursor, and similar tools.

Two install paths from one source of truth:

- **Claude Code users** can add the catalog directly with
  `/plugin marketplace add nulib-labs/nul-skills` and then
  `/plugin install <plugin>@nul-skills` — `nul-cli` is not required.
- **Codex, ChatGPT, Cursor, and other clients** use `nul skills add <plugin>`
  to install the plugin's skills into `.agents/skills/`.

`nul` also installs a generated `nul-cli` skill that documents the CLI's own
command tree.

## Installation

Installation via `uv`:

```bash
uv tool install nul-cli
nul --help
```

Run without installing:

```bash
uvx --from nul-cli nul --help
```

Installation via `pip`:

```bash
pip install nul-cli
nul --help
```

## Usage

```bash
nul --help
nul skills --help
```

List available plugins (community catalog + the generated `nul-cli` skill):

```bash
nul skills list
```

Preview a plugin's skill (defaults to the only skill if there's just one;
otherwise pass `<plugin>:<skill>`):

```bash
nul skills preview meadow
nul skills preview meadow:run-tests
```

Install the generated `nul-cli` skill:

```bash
nul skills add
```

Install all skills from a community plugin. Community skills are installed into
flat namespaced directories (`<plugin>-<skill>`) to avoid collisions across
plugins:

```bash
nul skills add meadow
# installs .agents/skills/meadow-run-tests/
```

Install one specific skill from a multi-skill plugin:

```bash
nul skills add meadow:run-tests
```

Upgrade installed skills (compares the recorded revision against the
upstream commit SHA at each plugin's path):

```bash
nul skills upgrade
```

## Community Catalog Source

The catalog is fetched from `nulib-labs/nul-skills` (branch `main`). Each
install records the resolved commit SHA in `.nul-skill-manifest.json`, and
`upgrade` re-resolves to detect new commits touching that plugin's path.

> **Naming note.** The catalog file is `.claude-plugin/marketplace.json` and
> each entry is a *plugin* with its own
> [`.claude-plugin/plugin.json`](https://docs.claude.com/en/docs/claude-code/plugins-reference)
> manifest. These are
> [Claude Code plugin marketplace](https://docs.claude.com/en/docs/claude-code/plugin-marketplaces)
> conventions, kept verbatim so the same `nul-skills` repo can be added as a
> Claude plugin source. We use "community" as the user-facing term throughout
> the CLI.
>
> Plugins may also bundle commands, hooks, or MCP servers for Claude Code.
> `nul-cli` ignores those and only installs SKILL.md files — non-Claude
> clients will use those skills via their own agent skill discovery.

Override via env vars:

- `NUL_SKILLS_REPO` — `<owner>/<repo>` to fetch from (default
  `nulib-labs/nul-skills`).
- `NUL_SKILLS_REF` — branch, tag, or commit SHA (default `main`).
- `NUL_SKILLS_PATH` — local checkout to use instead of fetching. Useful when
  iterating on skills locally.
- `GITHUB_TOKEN` / `GH_TOKEN` — used (if set) to lift GitHub API rate limits.

## Development

```bash
uv sync
uv run nul --help
uv run nul skills add --help
```

## Running Tests

```bash
uv run pytest
```

## Building

```bash
uv build
uv run twine check dist/*
```
