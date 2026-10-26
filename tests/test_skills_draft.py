"""Draft with Claude: the staging dir a skill is drafted in, and Save.

`draft_check` / `draft_start` / `draft_status` / `draft_save` / `draft_discard`
(skills.main actions) plus `draft_prompt`, the one place the task's prompt is
built. No network; HOME and FUSED_RENDER_HOME point into tmp_path.
"""
import os

import pytest

from fused_render.claude_config import lib, skills

GOOD = "---\nname: design-review\ndescription: Use when reviewing a page before shipping.\n---\n\n## Steps\n1. Look.\n"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    root = tmp_path / "claude-home"
    root.mkdir()
    monkeypatch.setattr(lib, "CLAUDE_DIR", str(root))
    monkeypatch.setattr(lib, "SETTINGS_PATH", str(root / "settings.json"))
    monkeypatch.setattr(lib, "_LOCK_PATH", str(root / ".config-ui.lock"))
    monkeypatch.setattr(skills, "SKILLS_DIR", str(root / "skills"))
    monkeypatch.setattr(skills, "_enabled_clis", lambda: ["claude", "codex"])
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "fr-home"))
    monkeypatch.delenv("COPILOT_HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return tmp_path


def _skill(parent, name="ui-design-review", extra=True):
    d = parent / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(GOOD)
    if extra:
        (d / "references").mkdir()
        (d / "references" / "tokens.md").write_text("tokens")
    return d


def _stage(text=GOOD):
    st = skills.main(action="draft_start")["staging"]
    if text is not None:
        with open(os.path.join(st, "SKILL.md"), "w") as f:
            f.write(text)
    return st


# -- draft_check ---------------------------------------------------------------


def test_check_accepts_a_folder_and_a_skill_md_file(home):
    d = _skill(home / "src")
    for target in (str(d), str(d / "SKILL.md")):
        res = skills.main(action="draft_check", path=target)
        assert res["ok"] is True
        assert res["root"] == str(d)
        assert res["name"] == "design-review"
        assert res["files"] == ["SKILL.md", "references/tokens.md"]


def test_check_resolves_tilde(home):
    _skill(home / ".claude" / "skills")
    res = skills.main(action="draft_check", path="~/.claude/skills/ui-design-review")
    assert res["ok"] is True and res["root"].endswith("ui-design-review")


def test_check_errors_are_results(home):
    (home / "empty").mkdir()
    for bad in ("", str(home / "nope"), str(home / "empty"), str(home / "empty" / "x.md")):
        res = skills.main(action="draft_check", path=bad)
        assert res["ok"] is False and res["error"]


# -- draft_start / status ------------------------------------------------------


def test_start_makes_a_staging_dir_under_the_state_dir_not_tmp(home):
    st = skills.main(action="draft_start")["staging"]
    root = os.path.join(str(home / "fr-home"), "skill-drafts")
    assert os.path.dirname(st) == root and os.path.isdir(st)
    assert os.listdir(st) == []
    assert skills.main(action="draft_start")["staging"] != st


def test_start_copies_the_source_in_as_the_starting_point(home):
    d = _skill(home / "src")
    res = skills.main(action="draft_start", path=str(d))
    assert res["ok"] is True
    assert sorted(skills._files(res["staging"])) == ["SKILL.md", "references/tokens.md"]
    assert (d / "SKILL.md").read_text() == GOOD  # the original is untouched


def test_start_with_a_bad_source_creates_nothing(home):
    res = skills.main(action="draft_start", path=str(home / "nope"))
    assert res["ok"] is False
    assert not os.path.exists(os.path.join(str(home / "fr-home"), "skill-drafts")) or not os.listdir(
        os.path.join(str(home / "fr-home"), "skill-drafts"))


def test_status_before_any_skill_md(home):
    st = _stage(None)
    res = skills.main(action="draft_status", staging=st)
    assert res == {"ok": True, "files": [], "text": None, "name": "", "description": "", "problems": []}


def test_status_reads_the_draft_and_validates_it(home):
    st = _stage()
    os.makedirs(os.path.join(st, "references"))
    open(os.path.join(st, "references", "a.md"), "w").write("x")
    res = skills.main(action="draft_status", staging=st)
    assert res["ok"] and res["text"] == GOOD
    assert res["name"] == "design-review"
    assert res["description"].startswith("Use when")
    assert res["files"] == ["SKILL.md", "references/a.md"]
    assert res["problems"] == []


def test_status_ignores_dot_claude_inside_the_staging_dir(home):
    st = _stage()
    os.makedirs(os.path.join(st, ".claude"))
    open(os.path.join(st, ".claude", "x.json"), "w").write("{}")
    assert skills.main(action="draft_status", staging=st)["files"] == ["SKILL.md"]


@pytest.mark.parametrize("text,needle", [
    ("---\ndescription: d\n---\n", "name"),
    ("---\nname: Bad Name\ndescription: d\n---\n", "name"),
    ("---\nname: " + "a" * 65 + "\ndescription: d\n---\n", "64"),
    ("---\nname: ok\n---\n", "description"),
    ("---\nname: ok\ndescription: " + "d" * 1025 + "\n---\n", "1024"),
    ("no frontmatter", "name"),
])
def test_status_lists_every_validation_problem(home, text, needle):
    res = skills.main(action="draft_status", staging=_stage(text))
    assert res["ok"] is True
    assert any(needle in p for p in res["problems"]), res["problems"]


def test_status_flags_a_name_that_already_exists(home):
    _skill(home / "claude-home" / "skills", "design-review", extra=False)
    res = skills.main(action="draft_status", staging=_stage())
    assert any("already exists" in p for p in res["problems"])


# -- draft_save ----------------------------------------------------------------


def test_save_all_places_under_agents_with_a_claude_link_and_removes_staging(home):
    st = _stage()
    os.makedirs(os.path.join(st, "references"))
    open(os.path.join(st, "references", "a.md"), "w").write("x")
    res = skills.main(action="draft_save", staging=st, placement="all")
    assert res["ok"] is True and res["slug"] == "design-review"
    dest = home / ".agents" / "skills" / "design-review"
    assert (dest / "SKILL.md").read_text() == GOOD
    assert (dest / "references" / "a.md").read_text() == "x"
    assert (home / "claude-home" / "skills" / "design-review").is_symlink()
    assert os.path.realpath(res["key"]) == os.path.realpath(dest)
    assert not os.path.exists(st)


def test_save_claude_only_places_under_claude_skills(home):
    st = _stage()
    res = skills.main(action="draft_save", staging=st, placement="claude")
    assert res["ok"] is True
    dest = home / "claude-home" / "skills" / "design-review"
    assert (dest / "SKILL.md").exists() and not dest.is_symlink()
    assert not (home / ".agents").exists()
    assert os.path.realpath(res["key"]) == os.path.realpath(dest)


def test_save_does_not_copy_dot_claude(home):
    st = _stage()
    os.makedirs(os.path.join(st, ".claude"))
    open(os.path.join(st, ".claude", "x.json"), "w").write("{}")
    skills.main(action="draft_save", staging=st, placement="claude")
    assert not (home / "claude-home" / "skills" / "design-review" / ".claude").exists()


def test_save_refuses_an_invalid_draft_and_keeps_staging(home):
    st = _stage("---\nname: Bad Name\ndescription: d\n---\n")
    res = skills.main(action="draft_save", staging=st, placement="all")
    assert res["ok"] is False and res["problems"]
    assert os.path.isdir(st)


def test_save_never_overwrites_an_existing_skill(home):
    _skill(home / "claude-home" / "skills", "design-review", extra=False)
    st = _stage()
    res = skills.main(action="draft_save", staging=st, placement="claude")
    assert res["ok"] is False and "already exists" in res["error"]
    assert (home / "claude-home" / "skills" / "design-review" / "SKILL.md").read_text() == GOOD
    assert os.path.isdir(st)


def test_save_rejects_an_unknown_placement(home):
    res = skills.main(action="draft_save", staging=_stage(), placement="everywhere")
    assert res["ok"] is False


# -- discard + traversal -------------------------------------------------------


def test_discard_removes_staging(home):
    st = _stage()
    assert skills.main(action="draft_discard", staging=st) == {"ok": True}
    assert not os.path.exists(st)
    assert skills.main(action="draft_discard", staging=st)["ok"] is True  # idempotent


def test_every_staging_arg_must_live_in_the_drafts_root(home):
    st = _stage()
    victim = home / "victim"
    victim.mkdir()
    (victim / "SKILL.md").write_text(GOOD)
    root = os.path.dirname(st)
    for bad in (str(victim), os.path.join(root, "..", "victim"), root, "", "relative",
                os.path.join(st, "..", "..", "victim"), os.path.join(st, "sub")):
        for action in ("draft_status", "draft_discard"):
            res = skills.main(action=action, staging=bad)
            assert res["ok"] is False, (action, bad)
        res = skills.main(action="draft_save", staging=bad, placement="claude")
        assert res["ok"] is False, bad
    assert victim.exists() and os.path.isdir(st)


def test_a_symlinked_staging_dir_is_rejected(home):
    st = _stage()
    link = os.path.join(os.path.dirname(st), "linked")
    os.symlink(str(home), link)
    assert skills.main(action="draft_discard", staging=link)["ok"] is False
    assert home.exists()


# -- the prompt ----------------------------------------------------------------


def test_prompt_carries_the_description_verbatim_and_the_rules():
    ask = 'Review pages.\nCheck "colors" & spacing.'
    p = skills.draft_prompt(ask, adapting=False)
    assert ask in p
    assert "SKILL.md" in p and "name" in p and "description" in p
    assert "Use when" in p
    assert "current directory" in p
    assert "references/" in p and "scripts/" in p
    assert "outside" in p
    assert "already contains" not in p


def test_prompt_for_an_adaptation_says_so():
    p = skills.draft_prompt("make it shorter", adapting=True)
    assert "make it shorter" in p
    assert "already contains" in p


def test_main_exposes_the_prompt_as_an_action():
    res = skills.main(action="draft_prompt", text="Do X", adapting=True)
    assert res["ok"] is True and "Do X" in res["prompt"] and "already contains" in res["prompt"]
    assert "already contains" not in skills.main(action="draft_prompt", text="Do X")["prompt"]
