"""An expert showing its work in the chat (docs/experts.md §17): the two tools, the canvases it made, and what
its fence lets it write there."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import json
import os

import pytest

from fused_render.bots import agent_engine, registry
from fused_render.experts import sessions, show, store


@pytest.fixture
def expert():
    return registry.create(name="Test Expert", kind="expert", expert={"ask_me_about": "testing things"})


@pytest.fixture(autouse=True)
def kicks(monkeypatch):
    """The off-thread fetch of the workbench skills, recorded and never started (no thread, no git)."""
    from fused_render import canvases
    calls = []
    monkeypatch.setattr(canvases, "_kick_workbench_skills", lambda: calls.append(1))
    return calls


def _skills(root, names=("json-ui-schemas", "canvas-toml", "fused-udfs", "html-template-nodes")):
    """A loadable workbench plugin tree at `root` (the manifest and every skill `skill_plugin` requires)."""
    from fused_render import skill_plugin as sp
    os.makedirs(os.path.join(root, sp.MANIFEST_DIR), exist_ok=True)
    open(os.path.join(root, sp.MANIFEST_DIR, sp.MANIFEST_NAME), "w").write("{}")
    for name in set(sp.WORKBENCH_SKILLS) | set(names):
        os.makedirs(os.path.join(root, "skills", name), exist_ok=True)
        open(os.path.join(root, "skills", name, "SKILL.md"), "w").write(f"# {name}\n")
    open(os.path.join(root, "skills", "json-ui-schemas", "reference.md"), "w").write("## text\n")
    return str(root)


@pytest.fixture
def base(expert, tmp_path, monkeypatch):
    """A canvases base granted to the expert, the `fused` wrapper and the Canvases feature on."""
    from fused_render import canvases, fusedcli
    from fused_render.shell import prefs
    root = tmp_path / "canvas"
    (root / "prod" / "user").mkdir(parents=True)
    monkeypatch.setattr(canvases, "canvases_base", lambda: str(root))
    monkeypatch.setenv(fusedcli.CLI_DIR_ENV, str(tmp_path / "fused-bin"))
    monkeypatch.setattr(prefs, "canvases_enabled", lambda: True)
    expert.meta["folders"] = [str(root)]
    return root


TOML = 'type = "canvas"\nversion = 2\nname = "{name}"\n\n[canvas]\nedges = []\n\n[[canvas.nodes]]\nudfName = "udf"\nx = 0.0\ny = 0.0\n'


def _fused(monkeypatch, base, fail=None):
    """The three canvases calls `canvas_create` makes, without a Fused: create is a no-op, the clone writes the
    folder a pull would."""
    from fused_render import canvases
    calls = []

    def create(body, x_fused=None):
        calls.append(("create", body["env"], body["name"], x_fused))
        return canvases._error("not signed in to Fused — sign in first", 409) if fail == "create" else {"ok": True}

    def clone(body, x_fused=None):
        calls.append(("clone", body["env"], body["name"], x_fused))
        d = base / body["env"] / "user" / body["name"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "canvas.toml").write_text(TOML.format(name=body["name"]))
        (d / "udf.py").write_text("@fused.udf\ndef udf():\n    return 1\n")
        return {"ok": True, "dir": str(d)}

    def start(env, name):
        calls.append(("start", env, name))
        return canvases._error("owned", 409) if fail == "start" else {"ok": True}
    monkeypatch.setattr(canvases, "api_canvases_create", create)
    monkeypatch.setattr(canvases, "api_canvases_clone", clone)
    monkeypatch.setattr(canvases, "_sync_start_response", start)
    return calls


def test_the_tools_are_offered_only_where_canvases_can_be_made(expert, base, monkeypatch):
    from fused_render.shell import prefs
    assert show.SERVER == agent_engine.MCP_SERVER
    assert show.on(expert) and [t["name"] for t in show.specs()] == ["canvas_create", "show"]
    assert "mcp__bot__canvas_create" in show.note(expert) and "mcp__bot__show" in show.note(expert)
    assert show.note(expert) in store.where(expert, run=True, show=True)
    assert show.note(expert) not in store.where(expert, run=True)  # an ask, a routine's turn: never told
    monkeypatch.setattr(prefs, "canvases_enabled", lambda: False)
    assert not show.on(expert) and show.note(expert) == "" and store.where(expert, run=True, show=True) == store.where(expert, run=True)
    monkeypatch.setattr(prefs, "canvases_enabled", lambda: True)
    expert.meta["folders"] = []  # no folder of canvas copies granted: no environment to make one in
    assert not show.on(expert)
    label, text, ok = show.canvas_create(expert, {"name": "A"})
    assert not ok and text.startswith("error: canvases are not available")


def test_fence_allows_the_two_tools_only_when_asked(expert, base, monkeypatch, tmp_path):
    plain = sessions.fence(expert, write=True, run=True, auto=True)
    assert not any("mcp__bot__" in x or "canvas validate" in x for x in plain)
    refs = tmp_path / "workbench"
    refs.mkdir()
    monkeypatch.setattr(show, "skills_root", lambda: str(refs))
    flags = sessions.fence(expert, write=True, run=True, auto=True, show=True)
    for rule in ("mcp__bot__canvas_create", "mcp__bot__show", "Bash(fused workbench --env prod canvas validate:*)",
                 f"Read(//{str(refs).lstrip('/')}/**)"):
        assert rule in flags, rule
    assert "mcp__bot__permission" not in flags and "mcp__bot__*" not in flags  # the prompt is never the model's
    assert flags[flags.index("--tools") + 1] == "Read,Glob,Grep,Edit,Write,Bash"  # no new built-in
    # The classifier is told about the canvases the expert made, and only once there are some.
    assert len(json.loads(flags[flags.index("--settings") + 1])["autoMode"]["allow"]) == 2
    show._note_made(expert, "prod", "Gone", str(base / "prod" / "user" / "Gone"))  # no such folder: not named
    (base / "prod" / "user" / "Mine").mkdir()
    show._note_made(expert, "prod", "Mine", str(base / "prod" / "user" / "Mine"))
    flags = sessions.fence(expert, write=True, run=True, auto=True, show=True)
    allow = json.loads(flags[flags.index("--settings") + 1])["autoMode"]["allow"]
    assert len(allow) == 3 and str(base / "prod" / "user" / "Mine") in allow[2]
    # The bot server's entry is always loaded for such a chat: an expert's `--tools` has no ToolSearch.
    cfg = agent_engine.write_mcp_config(str(tmp_path / "mcp.json"), "http://x", "b", "t", always_load=True)
    assert json.load(open(cfg))["mcpServers"]["bot"]["alwaysLoad"] is True
    cfg = agent_engine.write_mcp_config(str(tmp_path / "mcp.json"), "http://x", "b", "t")
    assert "alwaysLoad" not in json.load(open(cfg))["mcpServers"]["bot"]


def test_canvas_create_makes_clones_and_starts_sync(expert, base, monkeypatch):
    calls = _fused(monkeypatch, base)
    label, text, ok = show.canvas_create(expert, {"name": "Parcels_overview"})
    folder = str(base / "prod" / "user" / "Parcels_overview")
    assert ok and label == "create canvas Parcels_overview" and folder in text
    assert "canvas validate" in text and "sync has not started" not in text
    # The Canvases page's own three calls, in its order, with the header its routes require.
    assert calls == [("create", "prod", "Parcels_overview", "1"), ("clone", "prod", "Parcels_overview", "1"),
                     ("start", "prod", "Parcels_overview")]
    assert [(r["env"], r["name"], r["dir"]) for r in show.made(expert)] == [("prod", "Parcels_overview", folder)]
    # Asked again for its own canvas: nothing is created twice, the folder comes back.
    del calls[:]
    assert show.canvas_create(expert, {"name": "Parcels_overview", "env": "prod"})[2]
    assert calls == [("start", "prod", "Parcels_overview")] and len(show.made(expert)) == 1


@pytest.mark.parametrize("args, said", [
    ({"name": "has space"}, "letters, digits and underscores"),
    ({"name": "../x"}, "letters, digits and underscores"),
    ({"name": ""}, "letters, digits and underscores"),
    ({"name": "A", "env": "custom"}, "give `env`, one of: prod"),
    ({"name": "Theirs"}, "already has a canvas named Theirs"),
])
def test_canvas_create_refuses(expert, base, monkeypatch, args, said):
    calls = _fused(monkeypatch, base)
    (base / "prod" / "user" / "Theirs").mkdir()  # a canvas of the user's the expert did not make
    label, text, ok = show.canvas_create(expert, args)
    assert not ok and text.startswith("error:") and said in text
    assert calls == [] and show.made(expert) == []


def test_canvas_create_reports_what_fused_said(expert, base, monkeypatch):
    calls = _fused(monkeypatch, base, fail="create")
    label, text, ok = show.canvas_create(expert, {"name": "A"})
    assert not ok and "not signed in to Fused" in text and [c[0] for c in calls] == ["create"]
    assert show.made(expert) == [] and not (base / "prod" / "user" / "A").exists()
    # A sync another fused-render owns does not fail the tool: the card's view starts it.
    _fused(monkeypatch, base, fail="start")
    label, text, ok = show.canvas_create(expert, {"name": "B"})
    assert ok and "sync has not started yet" in text


def test_only_node_files_of_a_made_canvas_are_written_without_a_card(expert, base, monkeypatch, tmp_path):
    _fused(monkeypatch, base)
    assert show.canvas_create(expert, {"name": "Mine"})[2]
    mine, theirs = base / "prod" / "user" / "Mine", base / "prod" / "user" / "Theirs"
    theirs.mkdir()
    (theirs / "canvas.toml").write_text(TOML.format(name="Theirs"))
    for ok in ("chart.json", "load.py", "canvas.toml", "notes.md", "page.html"):
        assert store.free_write(expert, str(mine / ok)), ok
    for no in (".fused-canvas.json", ".fusedignore", "CLAUDE.md", "sub/x.py", "../Theirs/x.py", ""):
        assert not store.free_write(expert, str(mine / no)), no
    assert not store.free_write(expert, str(mine))  # the folder itself is no file
    assert not store.free_write(expert, str(theirs / "x.py")) and not store.free_write(expert, str(theirs / "canvas.toml"))
    assert store.free_write(expert, "memory.md", store.expert_dir(expert))  # as before
    # A recorded folder that is not where that canvas's clone lives is not trusted (a hand-edited canvases.json).
    with open(show._made_path(expert), "w") as f:
        json.dump([{"env": "prod", "name": "Mine", "dir": str(theirs), "at": 1},
                   {"env": "custom", "name": "Theirs", "dir": str(theirs), "at": 1},
                   {"env": "prod", "name": "../Theirs", "dir": str(theirs), "at": 1}], f)
    assert not store.free_write(expert, str(theirs / "x.py")) and not store.free_write(expert, str(mine / "x.py"))
    # The folder replaced by a link elsewhere: judged on real paths.
    show._note_made(expert, "prod", "Linked", str(base / "prod" / "user" / "Linked"))
    os.symlink(str(tmp_path), str(base / "prod" / "user" / "Linked"))
    assert not store.free_write(expert, str(base / "prod" / "user" / "Linked" / "x.py"))
    # Without the tools (the feature turned off), a made canvas asks like any other file.
    show._note_made(expert, "prod", "Mine", str(mine))
    assert store.free_write(expert, str(mine / "a.py"))
    monkeypatch.setattr(show, "on", lambda bot: False)
    assert not store.free_write(expert, str(mine / "a.py"))


def test_show_says_what_a_path_is(expert, base, monkeypatch):
    _fused(monkeypatch, base)
    show.canvas_create(expert, {"name": "Mine"})
    mine = base / "prod" / "user" / "Mine"
    (mine / "chart.json").write_text("{}")
    label, text, view = show.show(expert, {"path": str(mine / "udf.py")})
    assert view == {"kind": "udf", "path": str(mine / "udf.py"), "title": "udf", "canvas": "Mine", "env": "prod"}
    assert label == "show udf.py" and "card" in text
    # A file with no row is not a node yet: the expert is told how to make it one.
    label, text, view = show.show(expert, {"path": str(mine / "chart.json")})
    assert view is None and "[[canvas.nodes]]" in text
    with open(mine / "canvas.toml", "a") as f:
        f.write('\n[[canvas.nodes]]\nudfName = "chart"\ntitle = "Sales by month"\nx = 300.0\ny = 0.0\n')
    assert show.show(expert, {"path": str(mine / "chart.json")})[2] == {
        "kind": "widget", "path": str(mine / "chart.json"), "title": "Sales by month", "canvas": "Mine", "env": "prod"}
    # The canvas itself, by its folder or its canvas.toml.
    whole = {"kind": "canvas", "path": str(mine), "title": "Mine", "canvas": "Mine", "env": "prod"}
    assert show.show(expert, {"path": str(mine)})[2] == whole
    assert show.show(expert, {"path": str(mine / "canvas.toml")})[2] == whole
    # Not showable: a missing file, a file elsewhere, a folder that is no canvas, a team copy.
    team = base / "prod" / "team" / "T"
    team.mkdir(parents=True)
    (team / "canvas.toml").write_text(TOML.format(name="T"))
    (team / "udf.py").write_text("x")
    for path in (str(mine / "gone.py"), store.memory_path(expert), str(base / "prod" / "user"), str(team),
                 str(team / "udf.py"), ""):
        label, text, view = show.show(expert, {"path": path})
        assert view is None and text.startswith("error:"), path


def test_the_showing_skill_is_one_text_in_every_shipped_expert():
    from fused_render.experts import seed
    keys = sorted(os.listdir(seed.SHIPPED_DIR))
    copies = {k: open(os.path.join(seed.SHIPPED_DIR, k, "skills", show.SKILL), encoding="utf-8").read() for k in keys}
    assert len(keys) >= 2 and len(set(copies.values())) == 1  # edit them together
    for k in keys:
        text = open(os.path.join(seed.SHIPPED_DIR, k, "instructions.md"), encoding="utf-8").read()
        assert "## Showing your work" in text and f"skills/{show.SKILL}" in text


# ------------------------------------------------------------- the workbench skills (format references) ---
def _published(monkeypatch):
    """A staged generation of the skills, published the way `skill_plugin.export_workbench_plugin_env` does."""
    from fused_render import skill_plugin as sp
    gen = _skills(os.path.join(sp.workbench_staged_dir(), "g-abc"))
    monkeypatch.setenv(sp.WORKBENCH_PLUGIN_DIR_ENV, gen)
    return gen


def test_skills_root_is_the_spliced_generation_sessions_get(monkeypatch, tmp_path):
    from fused_render import skill_plugin as sp
    monkeypatch.delenv(sp.WORKBENCH_PLUGIN_DIR_ENV, raising=False)
    monkeypatch.setattr(sp, "workbench_plugin_root", lambda: None)
    assert show.skills_root() == ""  # nothing fetched yet
    gen = _published(monkeypatch)
    assert show.skills_root() == gen  # the staged copy, never the raw clone beside it
    # A clone fetched (by another process) but nothing published in this one: staged and published now.
    monkeypatch.delenv(sp.WORKBENCH_PLUGIN_DIR_ENV)
    monkeypatch.setattr(sp, "workbench_plugin_root", lambda: str(tmp_path / "clone"))
    monkeypatch.setattr(sp, "export_workbench_plugin_env", lambda: gen)
    assert show.skills_root() == gen
    # A published path that is no longer a complete tree is not handed out.
    monkeypatch.setattr(sp, "workbench_plugin_root", lambda: None)
    monkeypatch.setenv(sp.WORKBENCH_PLUGIN_DIR_ENV, str(tmp_path / "gone"))
    assert show.skills_root() == ""


def test_the_fence_reads_every_generation_and_kicks_the_fetch(expert, base, monkeypatch, kicks, tmp_path):
    from fused_render import skill_plugin as sp
    monkeypatch.delenv(sp.WORKBENCH_PLUGIN_DIR_ENV, raising=False)
    monkeypatch.setattr(sp, "workbench_plugin_root", lambda: None)
    parent = f"Read(//{sp.workbench_staged_dir().lstrip('/')}/**)"
    # Nothing fetched yet: the rule is there anyway (the skills arrive after the spawn), and the fetch is kicked.
    flags = sessions.fence(expert, write=True, run=True, auto=True, show=True)
    assert parent in flags and kicks == [1]
    assert not any("workbench-plugin/g-" in f for f in flags)  # never one generation: a refresh replaces it
    # `json-ui validate` uploads the file it is given: never pre-allowed.
    assert not any("json-ui validate" in x for x in flags)
    for cmd in ("canvas validate", "json-ui schemas"):
        assert f"Bash(fused workbench --env prod {cmd}:*)" in flags
    assert not kicks[1:] and sessions.fence(expert, write=True, run=True, auto=True)  # no kick without the tools
    assert kicks == [1]
    _published(monkeypatch)
    flags = sessions.fence(expert, write=True, run=True, auto=True, show=True)
    assert [f for f in flags if f.startswith("Read(") and "workbench" in f] == [parent]
    # A root outside the staged folder (the raw clone, when staging failed) gets its own rule; a glob in it, none.
    other = _skills(tmp_path / "clone" / "workbench")
    monkeypatch.setenv(sp.WORKBENCH_PLUGIN_DIR_ENV, other)
    assert f"Read(//{other.lstrip('/')}/**)" in sessions.fence(expert, write=True, run=True, auto=True, show=True)
    odd = _skills(tmp_path / "cl[o]ne" / "workbench")
    monkeypatch.setenv(sp.WORKBENCH_PLUGIN_DIR_ENV, odd)
    flags = sessions.fence(expert, write=True, run=True, auto=True, show=True)
    assert parent in flags and not any("cl[o]ne" in f for f in flags)


def test_the_expert_is_told_which_reference_to_read_for_each_file(expert, base, monkeypatch):
    from fused_render import skill_plugin as sp
    monkeypatch.delenv(sp.WORKBENCH_PLUGIN_DIR_ENV, raising=False)
    monkeypatch.setattr(sp, "workbench_plugin_root", lambda: None)
    line = show.note(expert)
    assert "not on this machine yet" in line and "canvas_create" in line and "workbench-plugin" not in line
    gen = _published(monkeypatch)
    line = show.note(expert)
    for rel in ("json-ui-schemas/SKILL.md", "json-ui-schemas/reference.md", "canvas-toml/SKILL.md",
                "fused-udfs/SKILL.md", "html-template-nodes/SKILL.md"):
        assert os.path.join(gen, "skills", rel) in line, rel
    assert "not on this machine yet" not in line and sp.workbench_staged_dir() in line  # where a newer one would be
    assert line in store.where(expert, run=True, show=True)
    # Only what is there is named.
    os.remove(os.path.join(gen, "skills", "json-ui-schemas", "reference.md"))
    assert "reference.md" not in show.note(expert) and "json-ui-schemas/SKILL.md" in show.note(expert)


class _Lock:
    """`canvases._SKILLS_FETCH_LOCK` as a spy: the timeout it is waited on with (rule 4 of the flake skill)."""
    def __init__(self, held=True, then=None):
        self.held, self.waits, self.then = held, [], then

    def locked(self):
        return self.held

    def acquire(self, timeout=-1):
        self.waits.append(timeout)
        if self.then:
            self.then()
        self.held = False
        return True

    def release(self):
        pass


def test_canvas_create_waits_for_the_references_and_names_them(expert, base, monkeypatch, kicks):
    from fused_render import canvases, skill_plugin as sp
    _fused(monkeypatch, base)
    monkeypatch.delenv(sp.WORKBENCH_PLUGIN_DIR_ENV, raising=False)
    monkeypatch.setattr(sp, "workbench_plugin_root", lambda: None)
    # None on this machine: the fetch in flight is waited for, bounded, and its result is named.
    lock = _Lock(then=lambda: _published(monkeypatch))
    monkeypatch.setattr(canvases, "_SKILLS_FETCH_LOCK", lock)
    label, text, ok = show.canvas_create(expert, {"name": "A"})
    assert ok and kicks and lock.waits == [show.SKILLS_WAIT_S]
    gen = sp.workbench_staged_dir() + os.sep + "g-abc"
    assert os.path.join(gen, "skills", "json-ui-schemas", "reference.md") in text
    assert os.path.join(gen, "skills", "canvas-toml", "SKILL.md") in text
    # Already there: no wait at all.
    lock.waits.clear()
    assert show.canvas_create(expert, {"name": "B"})[2] and lock.waits == []
    # The fetch failed: the tool still succeeds and says so.
    monkeypatch.delenv(sp.WORKBENCH_PLUGIN_DIR_ENV)
    monkeypatch.setattr(canvases, "_SKILLS_FETCH_LOCK", _Lock())
    label, text, ok = show.canvas_create(expert, {"name": "C"})
    assert ok and "could not be fetched" in text and "Do not guess" in text
    # A kick that raises fails nothing either.
    monkeypatch.setattr(canvases, "_kick_workbench_skills", lambda: 1 / 0)
    assert show.canvas_create(expert, {"name": "D"})[2]


# ---- the record of made canvases: what it may and may not vouch for ----
def _marker(folder, cid):
    from fused_render import canvas_marker
    canvas_marker.write_live(str(folder), name=folder.name, collection_id=cid)


def test_the_record_is_kept_outside_the_experts_folder(expert, base, monkeypatch):
    _fused(monkeypatch, base)
    assert show.canvas_create(expert, {"name": "Mine"})[2]
    path = os.path.realpath(show._made_path(expert))
    assert os.path.isfile(path) and not path.startswith(os.path.realpath(store.expert_dir(expert)) + os.sep)
    # A file of that name in the expert's own folder (where a shell command of its session could write) is not read.
    theirs = base / "prod" / "user" / "Theirs"
    theirs.mkdir()
    with open(os.path.join(store.state_dir(expert), "canvases.json"), "w") as f:
        json.dump([{"env": "prod", "name": "Theirs", "dir": str(theirs), "at": 1}], f)
    assert not store.free_write(expert, str(theirs / "x.py")) and not show.is_mine(expert, "prod", "Theirs")


def test_a_row_naming_the_users_canvas_at_its_real_place_is_not_believed(expert, base):
    theirs = base / "prod" / "user" / "Theirs"
    theirs.mkdir()
    _marker(theirs, "col-theirs")
    for forged in ({"env": "prod", "name": "Theirs", "dir": str(theirs), "at": 1},
                   {"env": "prod", "name": "Theirs", "dir": str(theirs), "at": 1, "id": "col-other"},
                   {"env": "prod", "name": "Theirs", "dir": str(theirs), "at": 1, "id": "", "ino": [0, 0]},
                   {"env": "prod", "name": "Theirs", "dir": str(theirs), "at": 1, "id": 7, "ino": "x"}):
        with open(show._made_path(expert), "w") as f:
            json.dump([forged], f)
        assert not store.free_write(expert, str(theirs / "x.py")), forged
        assert show.made_dirs(expert) == [] and not show.is_mine(expert, "prod", "Theirs")


def test_a_canvas_the_user_made_again_under_the_same_name_is_not_the_experts(expert, base, monkeypatch):
    """The expert made Demo; the user deleted it and made their own Demo. The old row must not vouch for it: its
    files ask, `canvas_create` refuses the name, and the classifier is not told the folder is the expert's."""
    import shutil
    calls = _fused(monkeypatch, base)
    assert show.canvas_create(expert, {"name": "Demo"})[2]
    demo = base / "prod" / "user" / "Demo"
    _marker(demo, "col-expert")
    show._note_made(expert, "prod", "Demo", str(demo))  # as recorded right after a real clone wrote its marker
    assert store.free_write(expert, str(demo / "a.py")) and show.is_mine(expert, "prod", "Demo")
    shutil.rmtree(demo)
    assert not show.is_mine(expert, "prod", "Demo") and show.made_dirs(expert) == []
    demo.mkdir()
    (demo / "canvas.toml").write_text(TOML.format(name="Demo"))
    _marker(demo, "col-user")
    assert not store.free_write(expert, str(demo / "a.py")) and not store.free_write(expert, str(demo / "canvas.toml"))
    assert not show.is_mine(expert, "prod", "Demo") and show.made_dirs(expert) == []
    del calls[:]
    label, text, ok = show.canvas_create(expert, {"name": "Demo"})
    assert not ok and "already has a canvas named Demo" in text and calls == []
    # A hard pull keeps the marker's id: the expert's own canvas stays its own across one.
    assert show.canvas_create(expert, {"name": "Kept"})[2]
    kept = base / "prod" / "user" / "Kept"
    _marker(kept, "col-kept")
    show._note_made(expert, "prod", "Kept", str(kept))
    shutil.rmtree(kept)
    kept.mkdir()
    _marker(kept, "col-kept")
    assert store.free_write(expert, str(kept / "a.py"))


def test_a_name_taken_on_fused_is_an_error_even_for_a_name_the_expert_once_used(expert, base, monkeypatch):
    import shutil
    _fused(monkeypatch, base)
    assert show.canvas_create(expert, {"name": "Demo"})[2]
    shutil.rmtree(base / "prod" / "user" / "Demo")
    calls = _fused(monkeypatch, base, fail="create")  # Fused refuses the create (signed out, or the name is taken)
    label, text, ok = show.canvas_create(expert, {"name": "Demo"})
    assert not ok and "was not created" in text and [c[0] for c in calls] == ["create"]  # never cloned, never recorded anew
    assert not (base / "prod" / "user" / "Demo").exists()


def test_only_node_suffixes_and_no_hard_links_are_card_free(expert, base, monkeypatch, tmp_path):
    _fused(monkeypatch, base)
    assert show.canvas_create(expert, {"name": "Mine"})[2]
    mine = base / "prod" / "user" / "Mine"
    for no in ("run.sh", "pyproject.toml", "canvas.toml.bak", "evil.pth", "x.so", "noext", "data.csv"):
        assert not store.free_write(expert, str(mine / no)), no
    outside = tmp_path / "outside.md"
    outside.write_text("the user's own file")
    os.link(str(outside), str(mine / "linked.md"))
    assert not store.free_write(expert, str(mine / "linked.md"))  # an edit would land on the other name too
    os.symlink(str(outside), str(mine / "sym.md"))
    assert not store.free_write(expert, str(mine / "sym.md"))
    (mine / "dir.py").mkdir()
    assert not store.free_write(expert, str(mine / "dir.py"))
    assert store.free_write(expert, str(mine / "udf.py")) and store.free_write(expert, str(mine / "new.json"))


def test_asking_again_for_its_own_canvas_says_nothing_new_was_made(expert, base, monkeypatch):
    _fused(monkeypatch, base)
    first = show.canvas_create(expert, {"name": "Mine"})[1]
    again = show.canvas_create(expert, {"name": "Mine"})[1]
    assert "is ready" in first and "starter node" in first
    assert "You already made the canvas Mine" in again and "starter node" not in again
