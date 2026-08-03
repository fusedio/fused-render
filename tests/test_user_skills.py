"""The user-level skill sync (fused_render/user_skills.py, D185): the three
canonical skills are installed into <CLAUDE_CONFIG_DIR>/skills/, refreshed on
every sync, marker-guarded so a user-authored skill with the same name is
never clobbered, and missing sources are skipped without raising.
"""
import os

import pytest

from fused_render import user_skills


@pytest.fixture()
def claude_dir(tmp_path, monkeypatch):
    d = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    return d


@pytest.fixture()
def sources(tmp_path, monkeypatch):
    """Point the module at a throwaway repo-skills dir with all three skills,
    and an absent packaged dir — the editable-install shape."""
    repo = tmp_path / "repo-skills"
    for name in user_skills.SKILLS:
        (repo / name).mkdir(parents=True)
        (repo / name / "SKILL.md").write_text(f"# {name}\n")
    monkeypatch.setattr(user_skills, "_REPO_SKILLS_DIR", str(repo))
    monkeypatch.setattr(
        user_skills, "_PACKAGED_SKILLS_DIR", str(tmp_path / "no-such-dir")
    )
    return repo


def test_sync_installs_all_skills_with_marker(claude_dir, sources):
    user_skills.sync_user_skills()
    for name in user_skills.SKILLS:
        target = claude_dir / "skills" / name
        assert (target / "SKILL.md").is_file()
        assert (target / user_skills._MARKER).is_file()


def test_sync_refreshes_a_managed_copy(claude_dir, sources):
    user_skills.sync_user_skills()
    target = claude_dir / "skills" / user_skills.SKILLS[0]
    (target / "SKILL.md").write_text("stale local edit\n")
    user_skills.sync_user_skills()
    assert (target / "SKILL.md").read_text().startswith("# fused-render")


def test_sync_never_touches_a_user_owned_skill(claude_dir, sources):
    """A same-named dir WITHOUT the marker is the user's — left byte-identical."""
    theirs = claude_dir / "skills" / user_skills.SKILLS[0]
    theirs.mkdir(parents=True)
    (theirs / "SKILL.md").write_text("my own skill\n")
    user_skills.sync_user_skills()
    assert (theirs / "SKILL.md").read_text() == "my own skill\n"
    assert not (theirs / user_skills._MARKER).exists()
    # the other skills still synced fine around it
    other = claude_dir / "skills" / user_skills.SKILLS[1]
    assert (other / user_skills._MARKER).is_file()


def test_sync_skips_missing_sources_without_raising(claude_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(user_skills, "_REPO_SKILLS_DIR", str(tmp_path / "gone"))
    monkeypatch.setattr(user_skills, "_PACKAGED_SKILLS_DIR", str(tmp_path / "gone2"))
    user_skills.sync_user_skills()  # must not raise
    assert not (claude_dir / "skills").exists()


def test_packaged_dir_is_the_wheel_fallback(claude_dir, tmp_path, monkeypatch):
    """Repo dir unresolvable (wheel install) -> the packaged copy is used."""
    packaged = tmp_path / "packaged-skills"
    name = user_skills.SKILLS[0]
    (packaged / name).mkdir(parents=True)
    (packaged / name / "SKILL.md").write_text("packaged\n")
    monkeypatch.setattr(user_skills, "_REPO_SKILLS_DIR", str(tmp_path / "gone"))
    monkeypatch.setattr(user_skills, "_PACKAGED_SKILLS_DIR", str(packaged))
    user_skills.sync_user_skills()
    assert (claude_dir / "skills" / name / "SKILL.md").read_text() == "packaged\n"


def test_the_real_repo_sources_resolve():
    """The module's default repo dir points at the actual skills/ — all three
    canonical skills exist there (guards the SKILLS tuple against a rename)."""
    for name in user_skills.SKILLS:
        assert os.path.isfile(
            os.path.join(user_skills._REPO_SKILLS_DIR, name, "SKILL.md")
        ), name
