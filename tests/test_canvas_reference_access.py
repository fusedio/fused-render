"""A session editing a canvas reads the reference canvases beside it (D1478).

`<env>/user/`, `<env>/team/` and `<env>/public/` hold every canvas the user
can reach, as working examples (D1476/D1477). What these pin:

* a session whose target is in a LIVE clone reads those three folders
  without a permission prompt, on every backend, each in its own currency
  (Claude: `Read(...)` rules, both spellings; Copilot: `--add-dir`; Gemini:
  `--include-directories`; OpenCode: named in its context; Codex: nothing,
  its sandbox reads everything) — and a target anywhere else, or inside a
  reference copy, gets none of it;
* the grant never reaches `extra_read_dirs`, so `_send` does not respawn;
* a folder not on disk yet is skipped, never handed to a CLI that would die
  on it;
* Claude cannot EDIT a reference copy: deny rules in `--settings`;
* every backend is told, in the appended prompt.

The server's half (the hook's deny) is in test_canvas_scoped_layout.py, the
seeded CLAUDE.md's in test_canvases.py.
"""
import json
import os

import pytest

from test_agent_backend import (  # noqa: F401 — fixtures are used by name
    SID_CLAUDE,
    _codex_bin,
    _copilot_bin,
    _gemini_bin,
    _inbox_rows,
    _live_run,
    _opencode_bin,
    _prefs,
    agent,
    home,
    send_env,
)

ENV = "unstable"
SCOPES = ("user", "team", "public")


def _marker(folder, sync, scope="user"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".fused-canvas.json").write_text(json.dumps({
        "version": 1, "sync": sync, "scope": scope, "id": "col-1", "name": folder.name,
        "owner": None, "revision": 1, "last_updated": None, "pulled_at": "t"}),
        encoding="utf-8")


class Layout:
    """`<root>/unstable/{user,team,public}` with one live clone, `demo`."""

    def __init__(self, agent, tmp_path, monkeypatch, *, root=None, scopes=SCOPES):
        self.root = root or (tmp_path / "canvas")
        self.env = self.root / ENV
        self.clone = self.env / "user" / "demo"
        self.clone.mkdir(parents=True)
        (self.clone / "canvas.toml").write_text('type = "canvas"\n', encoding="utf-8")
        (self.clone / "udf_1.py").write_text("def udf(): pass\n", encoding="utf-8")
        for scope in scopes:
            (self.env / scope).mkdir(exist_ok=True)
        monkeypatch.setattr(agent, "_canvases_root", lambda: str(self.root))

    def dirs(self, *scopes):
        return [str(self.env / s) for s in (scopes or SCOPES)]

    def reference(self, scope, name):
        """A reference copy: `team/x`, `public/y` or a mirror-marked `user/z`."""
        folder = self.env / scope / name
        _marker(folder, "mirror", scope)
        (folder / "canvas.toml").write_text('type = "canvas"\n', encoding="utf-8")
        (folder / "udf_1.py").write_text("def udf(): pass\n", encoding="utf-8")
        return folder


@pytest.fixture
def layout(agent, tmp_path, monkeypatch):
    return Layout(agent, tmp_path, monkeypatch)


# -- which folders, for which target -------------------------------------------


def test_only_a_live_clone_target_has_reference_dirs(agent, layout, tmp_path):
    """The one helper every backend asks. The three scope folders of the
    target's OWN environment, most specific first; nothing for a file
    elsewhere, a Fused Web clone, a reference copy, or a scope folder."""
    for target in (layout.clone, layout.clone / "udf_1.py", layout.clone / "new_node.py"):
        assert agent._canvas_reference_dirs(str(target)) == layout.dirs(), target
    # A live marker changes nothing; a mirror one makes it a reference copy.
    _marker(layout.clone, "live")
    assert agent._canvas_reference_dirs(str(layout.clone)) == layout.dirs()
    elsewhere = tmp_path / "proj" / "a.py"
    elsewhere.parent.mkdir()
    elsewhere.write_text("")
    web = layout.root / "webcanvas"                 # Fused Web: no env segment
    web.mkdir()
    refs = [layout.reference("team", "x"), layout.reference("public", "y"),
            layout.reference("user", "mine")]
    for target in (elsewhere, web, layout.env, layout.env / "user", layout.env / "team",
                   *refs, *(r / "udf_1.py" for r in refs), ""):
        assert agent._canvas_reference_dirs(str(target)) == [], target
        assert agent._canvas_reference_dirs_on_disk(str(target)) == [], target
    # Another environment's clone gets ITS folders, never this one's.
    other = layout.root / "prod" / "user" / "demo"
    other.mkdir(parents=True)
    assert agent._canvas_reference_dirs(str(other)) == [
        str(layout.root / "prod" / s) for s in SCOPES]


def test_the_scope_folders_are_named_before_the_mirror_made_them(agent, tmp_path, monkeypatch):
    """The mirror run creates `team/` and `public/`, and may not have run. A
    Claude rule needs no directory; the on-disk list is what the others get."""
    lay = Layout(agent, tmp_path, monkeypatch, scopes=("user",))
    assert agent._canvas_reference_dirs(str(lay.clone)) == lay.dirs()
    assert agent._canvas_reference_dirs_on_disk(str(lay.clone)) == lay.dirs("user")
    (lay.env / "public").mkdir()
    assert agent._canvas_reference_dirs_on_disk(str(lay.clone)) == lay.dirs("user", "public")
    # A FILE where a scope folder should be is not a directory to hand over.
    (lay.env / "team").write_text("")
    assert agent._canvas_reference_dirs_on_disk(str(lay.clone)) == lay.dirs("user", "public")


def test_only_reference_folders_are_refused_to_edits(agent, layout, tmp_path):
    """`user/` is never in the deny list: the session's own clone is in it."""
    team, public = layout.dirs("team", "public")
    assert agent._canvas_readonly_dirs(str(layout.clone / "udf_1.py")) == [team, public]
    assert agent._canvas_readonly_dirs(str(layout.reference("team", "x"))) == [team, public]
    mine = layout.reference("user", "mine")
    assert agent._canvas_readonly_dirs(str(mine / "udf_1.py")) == [team, public, str(mine)]
    assert agent._canvas_readonly_dirs(str(tmp_path / "a.py")) == []
    assert agent._canvas_readonly_dirs(str(layout.env / "user")) == []


# -- Claude --------------------------------------------------------------------


def _claude_argv(agent, tmp_path, file, **kw):
    run_dir = tmp_path / "rd"
    (run_dir / "perm").mkdir(parents=True, exist_ok=True)
    args = dict(run_dir=str(run_dir), pane=False, cli_mode=None, session_id=SID_CLAUDE,
                model="", effort="", extra_read_dirs=[], file=str(file))
    args.update(kw)
    return agent._claude_argv(**args)


def _allowed(argv):
    assert argv.count("--allowed-tools") == 1
    return argv[argv.index("--allowed-tools") + 1].split(",")


def _settings(argv):
    assert argv.count("--settings") == 1
    return json.loads(argv[argv.index("--settings") + 1])


@pytest.fixture
def claude(agent, monkeypatch):
    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    return agent


def test_claude_reads_the_reference_folders_in_both_spellings(claude, tmp_path, monkeypatch):
    """The CLI matches a rule against the path's text, and the session
    meets these folders under either name: the root as the app spells it
    (here through a symlink, as `/tmp` is one on macOS) and as a tool that
    resolved it does."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link, target_is_directory=True)
    lay = Layout(claude, tmp_path, monkeypatch, root=link / "canvas")
    for target in (lay.clone, lay.clone / "udf_1.py"):
        argv = _claude_argv(claude, tmp_path, target)
        allowed = _allowed(argv)
        for scope in SCOPES:
            plain = str(link / "canvas" / ENV / scope)
            resolved = os.path.realpath(plain)
            assert plain != resolved
            assert "Read(//%s/**)" % plain.lstrip("/") in allowed, (scope, allowed)
            assert "Read(//%s/**)" % resolved.lstrip("/") in allowed, (scope, allowed)
        # Read rules only: nothing widens what the session may WRITE, and
        # `--add-dir` (workspace membership, edits included) is never used.
        assert "--add-dir" not in argv
        assert not [a for a in allowed if a.startswith(("Edit(", "Write("))]
        # The screenshots rule and the canvas tool are still there.
        assert claude._read_rule(claude.SHOTS) in allowed
        assert any(a.endswith("__" + claude.CANVAS_SHOT_TOOL) for a in allowed)


def test_claude_gets_no_reference_rules_outside_a_live_clone(claude, layout, tmp_path):
    elsewhere = tmp_path / "proj" / "a.py"
    elsewhere.parent.mkdir()
    elsewhere.write_text("")
    for target in (elsewhere, elsewhere.parent, layout.reference("team", "x") / "udf_1.py",
                   layout.reference("public", "y"), layout.reference("user", "mine")):
        allowed = _allowed(_claude_argv(claude, tmp_path, target))
        assert [a for a in allowed if a.startswith("Read(")] == [
            claude._read_rule(claude.SHOTS)], target


def test_claude_is_refused_edits_under_team_and_public(claude, layout, tmp_path):
    """the deny rules ride the same `--settings` as the canvas hooks.
    A deny holds under every permission mode."""
    for mode in (None, "acceptEdits", "auto"):
        settings = _settings(_claude_argv(claude, tmp_path, layout.clone / "udf_1.py",
                                          cli_mode=mode))
        deny = settings["permissions"]["deny"]
        for scope in ("team", "public"):
            assert "Edit(//%s/**)" % str(layout.env / scope).lstrip("/") in deny
        # Never the user's own folder, and never a blanket rule.
        assert not [r for r in deny if "/user/" in r or r.endswith("/user/**)")]
        assert "Edit" not in deny and "Write" not in deny
        # The canvas hooks are untouched by it.
        assert set(settings["hooks"]) == {"PreToolUse", "PostToolUse", "Stop",
                                          "UserPromptSubmit"}
        assert settings["hooks"]["PreToolUse"][0]["matcher"] == \
            "Write|Edit|MultiEdit|NotebookEdit"


def test_the_deny_rules_carry_both_spellings_too(claude, tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link, target_is_directory=True)
    lay = Layout(claude, tmp_path, monkeypatch, root=link / "canvas")
    deny = _settings(_claude_argv(claude, tmp_path, lay.clone))["permissions"]["deny"]
    assert deny == [
        "Edit(//%s/**)" % p.lstrip("/")
        for scope in ("team", "public")
        for p in (str(link / "canvas" / ENV / scope),
                  os.path.realpath(str(link / "canvas" / ENV / scope)))]


def test_a_claude_session_inside_a_reference_copy_is_read_only(claude, layout, tmp_path):
    """Decision 9: no canvas hooks, no canvas tool, the note — and the folder
    it sits in is refused to edits, `user/<mirror>` included."""
    mine = layout.reference("user", "mine")
    for ref in (layout.reference("team", "x"), mine):
        for target in (ref, ref / "udf_1.py"):
            argv = _claude_argv(claude, tmp_path, target)
            settings = _settings(argv)
            hooks = settings.get("hooks", {})
            assert set(hooks) <= {"PreToolUse"}, target       # the allow-once hook only
            assert all("canvas_hook" not in h["command"]
                       for entry in hooks.get("PreToolUse", []) for h in entry["hooks"])
            assert not any(a.endswith("__" + claude.CANVAS_SHOT_TOOL) for a in _allowed(argv))
            prompt = argv[argv.index("--append-system-prompt") + 1]
            assert "READ-ONLY reference copy" in prompt and "Canvases page" in prompt
            assert "REFERENCE CANVASES" not in prompt
            deny = settings["permissions"]["deny"]
            assert "Edit(//%s/**)" % str(layout.env / "team").lstrip("/") in deny
            assert ("Edit(//%s/**)" % str(mine).lstrip("/") in deny) == (ref == mine)
    # An ordinary file has no deny rules at all.
    assert "permissions" not in _settings(_claude_argv(claude, tmp_path, tmp_path / "a.py"))


def test_a_breakout_in_a_clone_keeps_its_own_denies(claude, layout, tmp_path):
    settings = _settings(_claude_argv(claude, tmp_path, layout.clone, breakout=True))
    deny = settings["permissions"]["deny"]
    assert "Bash" in deny and "Edit" in deny
    assert "Edit(//%s/**)" % str(layout.env / "team").lstrip("/") in deny
    assert len(deny) == len(set(deny))


# -- the other backends --------------------------------------------------------


def _flag_values(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def test_copilot_is_handed_the_folders_that_exist(agent, home, tmp_path, monkeypatch):
    """`--add-dir` for `team/` and `public/` where they are on disk; the CLI
    exits at startup on a missing one, so that never reaches it. Never
    `user/`: the grant is not read-only and nothing refuses a write, and
    that folder holds the user's other live clones."""
    _copilot_bin(tmp_path, monkeypatch)
    lay = Layout(agent, tmp_path, monkeypatch, scopes=("user", "public"))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    argv = agent._copilot_argv(str(run_dir), False, None, "", "", "", [],
                               str(lay.clone / "udf_1.py"))
    assert _flag_values(argv, "--add-dir") == [agent.SHOTS, *lay.dirs("public")]
    assert str(lay.env / "user") not in _flag_values(argv, "--add-dir")
    # Outside a live clone: the screenshots dir and nothing else.
    for target in (lay.reference("public", "y"), tmp_path / "a.py"):
        run = tmp_path / ("run-" + target.name)
        run.mkdir()
        argv = agent._copilot_argv(str(run), False, None, "", "", "", [], str(target))
        assert _flag_values(argv, "--add-dir") == [agent.SHOTS], target


def test_gemini_is_handed_the_folders_that_exist_and_hold_no_comma(
        agent, home, tmp_path, monkeypatch):
    """`--include-directories`, under the flag's two rules, for `team/` and
    `public/` only (a workspace directory is writable to Gemini's tools)."""
    _gemini_bin(tmp_path, monkeypatch)
    lay = Layout(agent, tmp_path, monkeypatch, scopes=("user", "team"))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    argv = agent._gemini_argv(str(run_dir), False, None, "", "", "", [], str(lay.clone))
    included = _flag_values(argv, "--include-directories")
    assert included == [str(run_dir / "gemini"), agent.SHOTS, *lay.dirs("team")]
    run2 = tmp_path / "run2"
    run2.mkdir()
    argv = agent._gemini_argv(str(run2), False, None, "", "", "", [], str(tmp_path / "a.py"))
    assert _flag_values(argv, "--include-directories") == [str(run2 / "gemini"), agent.SHOTS]
    # The CLI splits the flag's value on commas: a root with one in its path
    # is left out (and said so), never passed as two fragments.
    comma = Layout(agent, tmp_path, monkeypatch, root=tmp_path / "a, b" / "canvas")
    run3 = tmp_path / "run3"
    run3.mkdir()
    argv = agent._gemini_argv(str(run3), False, None, "", "", "", [], str(comma.clone))
    assert _flag_values(argv, "--include-directories") == [str(run3 / "gemini"), agent.SHOTS]


def test_opencode_names_the_folders_in_its_context(agent, home, tmp_path, monkeypatch):
    _opencode_bin(tmp_path, monkeypatch)
    lay = Layout(agent, tmp_path, monkeypatch, scopes=("user", "team"))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert agent._opencode_argv(str(run_dir), False, None, "", "", "", [],
                                str(lay.clone)) == [agent._opencode_bin(), "acp"]
    body = (run_dir / "opencode" / "AGENTS.md").read_text()
    listed = body[body.index("you may also read these directories: "):]
    assert str(lay.env / "team") in listed
    # Its tools are not workspace-bound and nothing refuses a write: the
    # folder of the user's other live clones is not named anywhere.
    assert str(lay.env / "user") + "," not in body and not body.rstrip().endswith(
        str(lay.env / "user"))
    assert str(lay.env / "user") not in listed.replace(str(lay.clone), "")
    assert str(lay.env / "public") not in body          # not on disk: not named
    run2 = tmp_path / "run2"
    run2.mkdir()
    agent._opencode_argv(str(run2), False, None, "", "", "", [], str(tmp_path / "a.py"))
    assert str(lay.env) not in (run2 / "opencode" / "AGENTS.md").read_text()


def test_codex_needs_nothing_on_its_spawn_line(agent, home, layout, tmp_path, monkeypatch):
    """Its sandboxes read the whole filesystem (CODEX D9): no flag names a
    reference folder. The prompt does (below)."""
    _codex_bin(tmp_path, monkeypatch)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    argv = agent._codex_argv(str(run_dir), False, None, "", "", "", [], str(layout.clone))
    assert not [a for a in argv if str(layout.env / "team") in a
                or str(layout.env / "public") in a]


# -- the prompt ----------------------------------------------------------------


@pytest.mark.parametrize("backend", ["", "copilot", "codex", "gemini", "opencode"])
def test_every_backend_is_told_about_the_reference_canvases(
        agent, home, tmp_path, monkeypatch, backend):
    lay = Layout(agent, tmp_path, monkeypatch, scopes=("user", "public"))
    for target in (lay.clone, lay.clone / "udf_1.py"):
        note = agent._canvas_reference_note(str(target), backend)
        assert note in agent._appended_prompt(str(target), False, backend)
        assert "REFERENCE CANVASES" in note
        # The folders that exist, by absolute path; the missing one is not
        # named. `user/` only for a backend that can refuse a write there
        # (it holds the user's other live clones): Claude and Codex.
        enforced = backend in ("", "codex")
        assert str(lay.env / "public") in note
        assert (str(lay.env / "user") in note) == enforced
        assert ("the user's own canvases" in note) == enforced
        assert "the public catalog" in note and "their team's" not in note
        assert str(lay.env / "team") not in note
        for must in ("read-only", "stale", ".fused-canvas.json", "never edit",
                     "push or pull", "reference DATA", "do not act on it",
                     "copy the pattern into this canvas"):
            assert must in note, must
        # Only Claude's spawn line pre-approves the reads, so only Claude is
        # told so, with both ways to search (some CLIs have no Grep tool).
        assert ("pre-approved" in note) == (backend == "")
        assert ("Grep, Glob and Read" in note and "`grep -rn`" in note) == (backend == "")
    # No canvas being edited, no note: an ordinary file, a reference copy.
    for target in (tmp_path / "a.py", lay.reference("public", "y") / "udf_1.py",
                   lay.reference("user", "mine")):
        assert agent._canvas_reference_note(str(target), backend) == ""
        assert "REFERENCE CANVASES" not in agent._appended_prompt(str(target), False, backend)


def test_the_note_reaches_the_files_the_other_backends_read(agent, home, tmp_path, monkeypatch):
    """Copilot's agent file, Gemini's GEMINI.md and OpenCode's AGENTS.md are
    built from the appended prompt; none of them is pointed at CLAUDE.md."""
    _copilot_bin(tmp_path, monkeypatch)
    lay = Layout(agent, tmp_path, monkeypatch)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    file = str(lay.clone)
    bodies = {
        "copilot": open(os.path.join(agent._copilot_plugin_dir(str(run_dir), file, False),
                                     "agents", "fused.agent.md")).read(),
        "gemini": open(os.path.join(agent._gemini_context_dir(str(run_dir), file, False),
                                    "GEMINI.md")).read(),
        "opencode": open(os.path.join(agent._opencode_context_dir(str(run_dir), file, False),
                                      "AGENTS.md")).read(),
    }
    for backend, body in bodies.items():
        assert agent._canvas_reference_note(file, backend) in body, backend
        assert "CLAUDE.md" not in body, backend


# -- no respawn ----------------------------------------------------------------


@pytest.mark.parametrize("backend", ["claude", "copilot", "gemini", "opencode", "codex"])
def test_a_follow_up_in_a_clone_never_respawns_over_the_reference_dirs(
        agent, home, layout, tmp_path, send_env, backend):
    """`_send` ends the session when a message names a directory the
    host was not granted (`wanted <= granted`). The reference folders are
    the TARGET's, computed inside each argv builder: the host's record of
    its grants (`read_dirs`, from `extra_read_dirs`) never held them, and a
    follow-up never asks for them."""
    _prefs(home, **{b + "_enabled": True for b in ("copilot", "codex", "gemini", "opencode")})
    file = str(layout.clone / "udf_1.py")
    assert agent._canvas_reference_dirs(file)            # this target HAS them
    run_dir = _live_run(agent, tmp_path, backend=backend,
                        mode="default" if backend != "copilot" else None)
    for name in ("host.json", "meta.json"):
        path = os.path.join(run_dir, name)
        with open(path) as fh:
            data = json.load(fh)
        data["file"] = file
        with open(path, "w") as fh:
            json.dump(data, fh)
    with open(os.path.join(run_dir, "host.json")) as fh:
        assert json.load(fh)["read_dirs"] == []          # what a real spawn records
    for _ in range(2):
        assert agent._send("run-1", "more") == {"sent": True}
    assert send_env == []                                # `_cancel` never ran
    assert [r["type"] for r in _inbox_rows(run_dir)] == ["user", "user"]
    # The directories a message asks for come from the message alone.
    assert agent._attach_dirs("") == []


# -- one rule for who is given `user/`, and deny rules for the copies in it ------


def test_only_a_backend_that_can_refuse_a_write_is_given_the_users_own_folder(
        agent, layout):
    """`user/` holds the user's OTHER live clones, each published by its own
    sync. Claude (deny rules, the hook) and Codex (its sandbox) get all
    three folders; Copilot, Gemini and OpenCode get `team/` and `public/`,
    where a stray write is overwritten by the next mirror run."""
    file = str(layout.clone / "udf_1.py")
    assert agent._canvas_reference_scopes("") == ("user", "team", "public")
    for backend in ("", "claude", "codex"):
        assert agent._canvas_reference_dirs(file, backend) == layout.dirs(
            "user", "team", "public"), backend
    for backend in ("copilot", "gemini", "opencode", "something-new"):
        assert agent._canvas_reference_scopes(backend) == ("team", "public")
        assert agent._canvas_reference_dirs(file, backend) == layout.dirs("team", "public")
        assert agent._canvas_reference_dirs_on_disk(file, backend) == layout.dirs(
            "team", "public")


def test_claude_denies_edits_to_the_reference_copies_under_user_that_exist_at_spawn(
        agent, layout, monkeypatch):
    """`user/` cannot be denied whole (the session's own clone is in it), so
    each reference copy in it gets a rule of its own: a deny also stops a
    shell redirect, which the hook does not see. Live clones get none."""
    mine = layout.reference("user", "mine")
    other = layout.reference("user", "another")
    sibling = layout.env / "user" / "sibling_clone"
    sibling.mkdir()
    (sibling / "canvas.toml").write_text("")
    # A copy whose adoption has not been pulled yet is still a copy.
    half = layout.env / "user" / "half_adopted"
    half.mkdir()
    (half / "canvas.toml").write_text("")
    (layout.env / ".sync").mkdir(exist_ok=True)
    (layout.env / ".sync" / "half_adopted.adopting").write_text("1")
    dirs = agent._canvas_readonly_dirs(str(layout.clone / "udf_1.py"))
    assert dirs == [*layout.dirs("team", "public"), str(other), str(half), str(mine)]
    assert str(sibling) not in dirs and str(layout.clone) not in dirs
    # Bounded: by name, at most the cap; the rest keep the hook and the prompt.
    monkeypatch.setattr(agent, "_REFERENCE_DENY_CAP", 2)
    assert agent._canvas_readonly_dirs(str(layout.clone)) == [
        *layout.dirs("team", "public"), str(other), str(half)]
    # A session started inside a reference copy: its own folder, as before.
    assert agent._canvas_readonly_dirs(str(mine / "udf_1.py")) == [
        *layout.dirs("team", "public"), str(mine)]
