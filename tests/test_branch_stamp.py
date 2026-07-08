import json
import shutil
from pathlib import Path

from fused_render._branch import branch_port
from scripts import branch_stamp

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_fixtures(tmp_path):
    marketplace_dir = tmp_path / ".claude-plugin"
    marketplace_dir.mkdir()
    shutil.copy(
        REPO_ROOT / ".claude-plugin" / "marketplace.json",
        marketplace_dir / "marketplace.json",
    )
    shutil.copy(
        REPO_ROOT / ".claude-plugin" / "plugin.json",
        marketplace_dir / "plugin.json",
    )

    skills_dir = tmp_path / "skills"
    skill_files = []
    for name in (
        "fused-render-usage",
        "fused-render-authoring",
        "fused-render-custom-templates",
    ):
        d = skills_dir / name
        d.mkdir(parents=True)
        dest = d / "SKILL.md"
        shutil.copy(REPO_ROOT / "skills" / name / "SKILL.md", dest)
        skill_files.append(dest)

    return marketplace_dir / "marketplace.json", marketplace_dir / "plugin.json", skill_files


def test_stamp_with_ref(tmp_path):
    marketplace_path, plugin_path, skill_files = _make_fixtures(tmp_path)

    branch_stamp.stamp(
        ref="foo",
        marketplace_path=marketplace_path,
        plugin_path=plugin_path,
        skill_files=skill_files,
    )

    marketplace = json.loads(marketplace_path.read_text())
    assert marketplace["name"] == "fused-render-foo"
    assert marketplace["plugins"][0]["name"] == "fused-render-foo"

    plugin = json.loads(plugin_path.read_text())
    assert plugin["name"] == "fused-render-foo"
    assert plugin["displayName"] == "Fused Render (foo)"

    expected_port = str(branch_port("foo"))
    for f in skill_files:
        text = f.read_text()
        assert "8765" not in text
        assert expected_port in text


def test_stamp_reset_round_trips_to_baseline(tmp_path):
    marketplace_path, plugin_path, skill_files = _make_fixtures(tmp_path)

    orig_marketplace = marketplace_path.read_text()
    orig_plugin = plugin_path.read_text()
    orig_skills = {f: f.read_text() for f in skill_files}

    branch_stamp.stamp(
        ref="foo",
        marketplace_path=marketplace_path,
        plugin_path=plugin_path,
        skill_files=skill_files,
    )
    branch_stamp.stamp(
        ref="",
        marketplace_path=marketplace_path,
        plugin_path=plugin_path,
        skill_files=skill_files,
    )

    assert marketplace_path.read_text() == orig_marketplace
    assert plugin_path.read_text() == orig_plugin
    for f in skill_files:
        assert f.read_text() == orig_skills[f]

    marketplace = json.loads(marketplace_path.read_text())
    assert marketplace["name"] == "fused-render"
    plugin = json.loads(plugin_path.read_text())
    assert plugin["name"] == "fused-render"
    assert plugin["displayName"] == "Fused Render"
    for f in skill_files:
        assert "8765" in f.read_text()


def test_stamp_idempotent(tmp_path):
    marketplace_path, plugin_path, skill_files = _make_fixtures(tmp_path)

    branch_stamp.stamp(
        ref="foo",
        marketplace_path=marketplace_path,
        plugin_path=plugin_path,
        skill_files=skill_files,
    )
    first_marketplace = marketplace_path.read_text()
    first_plugin = plugin_path.read_text()
    first_skills = {f: f.read_text() for f in skill_files}

    branch_stamp.stamp(
        ref="foo",
        marketplace_path=marketplace_path,
        plugin_path=plugin_path,
        skill_files=skill_files,
    )

    assert marketplace_path.read_text() == first_marketplace
    assert plugin_path.read_text() == first_plugin
    for f in skill_files:
        assert f.read_text() == first_skills[f]

    marketplace = json.loads(marketplace_path.read_text())
    assert marketplace["name"] == "fused-render-foo"
    plugin = json.loads(plugin_path.read_text())
    assert plugin["name"] == "fused-render-foo"
