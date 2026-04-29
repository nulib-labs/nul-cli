from __future__ import annotations

import json
from pathlib import Path

import pytest

from nul_cli import _skills


@pytest.fixture(autouse=True)
def community_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    catalog_root = tmp_path / "nul-skills"
    plugin_root = catalog_root / "plugins" / "meadow"
    (catalog_root / ".claude-plugin").mkdir(parents=True)
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    (plugin_root / "skills" / "run-tests").mkdir(parents=True)

    (catalog_root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "nul-skills",
                "owner": {"name": "Northwestern University Libraries"},
                "plugins": [
                    {
                        "name": "meadow",
                        "source": "./plugins/meadow",
                        "description": "Run Meadow tests.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "meadow",
                "description": "Run Meadow tests.",
                "author": {"name": "Northwestern University Libraries"},
            }
        ),
        encoding="utf-8",
    )
    (plugin_root / "skills" / "run-tests" / "SKILL.md").write_text(
        "---\nname: run-tests\ndescription: Run Meadow tests.\n---\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("NUL_SKILLS_PATH", str(catalog_root))
    return catalog_root


def test_lists_community_plugin() -> None:
    plugins = _skills.list_community_plugins()

    assert ["meadow"] == [p.name for p in plugins]


def test_remote_revision_uses_github_path_query_param(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NUL_SKILLS_PATH", raising=False)
    captured: dict[str, str] = {}

    def fake_http_get(url: str, *, accept: str | None = None) -> bytes:
        captured["url"] = url
        captured["accept"] = accept or ""
        return b'[{"sha": "abc123"}]'

    monkeypatch.setattr(_skills, "_http_get", fake_http_get)

    revision = _skills.resolve_plugin_revision(
        _skills.CommunityPlugin(name="meadow", source="./plugins/meadow")
    )

    assert revision == "abc123"
    assert captured["accept"] == "application/vnd.github+json"
    assert "https://api.github.com/repos/nulib-labs/nul-skills/commits?" in captured["url"]
    assert "path=plugins%2Fmeadow" in captured["url"]
    assert "sha=main" in captured["url"]


def test_install_community_plugin_writes_manifest(tmp_path: Path) -> None:
    installed = _skills.install_community_plugin("meadow", tmp_path)

    assert 1 == len(installed)
    target = installed[0]
    assert "meadow-run-tests" == target.name
    manifest = json.loads((target / _skills.MANIFEST_FILE).read_text(encoding="utf-8"))

    assert (target / "SKILL.md").exists()
    assert "meadow" == manifest["plugin"]
    assert "run-tests" == manifest["name"]
    assert "plugins/meadow" == manifest["source"]
    assert manifest["revision"].startswith(_skills.LOCAL_REVISION_PREFIX)
    assert _skills.MANIFEST_SCHEMA_VERSION == manifest["schema_version"]


def test_install_skill_with_explicit_skill_selector(tmp_path: Path) -> None:
    installed = _skills.install_skill("meadow:run-tests", tmp_path)

    assert [tmp_path / "meadow-run-tests"] == [p.resolve() for p in installed]


def test_apply_updates_reports_up_to_date_after_install(tmp_path: Path) -> None:
    _skills.install_community_plugin("meadow", tmp_path)

    results = _skills.apply_updates([tmp_path])

    assert 1 == len(results)
    assert "run-tests" == results[0].name
    assert "up-to-date" == results[0].status


def test_apply_updates_selector_uses_installed_directory_name(tmp_path: Path) -> None:
    _skills.install_community_plugin("meadow", tmp_path)

    results = _skills.apply_updates([tmp_path], selector="meadow-run-tests")

    assert 1 == len(results)
    assert "run-tests" == results[0].name
    assert "up-to-date" == results[0].status


def test_install_nul_cli_skill_writes_dynamic_md(tmp_path: Path) -> None:
    target = _skills.install_nul_cli_skill(tmp_path)
    manifest = json.loads((target / _skills.MANIFEST_FILE).read_text(encoding="utf-8"))

    skill_md = (target / "SKILL.md").read_text(encoding="utf-8")
    assert skill_md.startswith("---\nname: nul-cli\n")
    assert manifest["name"] == "nul-cli"
    assert manifest["plugin"] == "nul-cli"
    assert manifest["revision"].startswith("nul-cli@")
