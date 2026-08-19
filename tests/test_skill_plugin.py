"""The skill PLUGIN root (fused_render/skill_plugin.py, D216): the canonical
skills assembled into `home_dir()/skill-plugin/` in the shape Claude Code's
`--plugin-dir` loader wants, and handed to every session fused-render spawns.

Three groups of properties, and they fail in different places:

* **the assembled shape** — a manifest at `.claude-plugin/plugin.json` and one
  dir per skill under `skills/`. Get this wrong and the CLI ignores the whole
  root silently; the session just doesn't know the bridge contract.
* **the packaging invariant** — nothing in the wheel lives under a dot-prefixed
  path (which is why the packaged manifest is a flat `skills/plugin.json`).
  A dotted path that the build backend's globs quietly drop is a failure you
  only ever see in a built wheel on a user's machine.
* **the seams** — `SKILLS` and the source roots exist here AND in
  `user_skills.py`; the root reaches the two claude templates (which may not
  import the package, SPEC PY-15) only as an env var, decision already made
  server-side. Each seam is pinned below, so the ends cannot drift apart
  unnoticed.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import threading
import time

import pytest

from fused_render import skill_plugin, user_skills

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A private home dir, so the sync writes under tmp and not the real one
    (conftest already redirects FUSED_RENDER_HOME for the whole run; this
    narrows it per test so the stamp short-circuit is testable in isolation).

    Returns the plugin root's parent as the SERVER resolves it — `home_dir()`
    nests under `branches/<ref>/` when a branch ref is baked in, so a test that
    spelled out `tmp/home/skill-plugin` would pass only on a baseline build.
    `FUSED_RENDER_HOME_DIR` carries that already-resolved answer to the
    templates, exactly as `server.export_app_env` does in production."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    from fused_render.shell.storage import home_dir

    resolved = home_dir()
    monkeypatch.setenv("FUSED_RENDER_HOME_DIR", resolved)
    return resolved


@pytest.fixture
def sources(tmp_path, monkeypatch):
    """Fake repo-level sources: one dir per skill plus a manifest, with the
    packaged fallback pointed at nothing so resolution order is unambiguous."""
    repo = tmp_path / "repo"
    for name in skill_plugin.SKILLS:
        (repo / "skills" / name).mkdir(parents=True)
        (repo / "skills" / name / "SKILL.md").write_text(
            f"# {name}\n", encoding="utf-8")
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "fused-render", "description": "d"}), encoding="utf-8")
    monkeypatch.setattr(skill_plugin, "_REPO_SKILLS_DIR", str(repo / "skills"))
    monkeypatch.setattr(
        skill_plugin, "_REPO_MANIFEST",
        str(repo / ".claude-plugin" / "plugin.json"))
    monkeypatch.setattr(skill_plugin, "_PACKAGED_SKILLS_DIR",
                        str(tmp_path / "no-such-dir"))
    monkeypatch.setattr(skill_plugin, "_PACKAGED_MANIFEST",
                        str(tmp_path / "no-such-dir" / "plugin.json"))
    return repo


# --------------------------------------------- concurrency and partial trees

def test_a_gutted_root_is_rebuilt_rather_than_trusted(home, sources):
    """A manifest is not evidence of a complete tree. Left unchecked, a root that
    lost its skills — an interrupted build, a concurrent rebuild — still looked
    loadable, and a matching stamp then short-circuited every later sync, so
    sessions kept being handed a plugin that teaches the model nothing until the
    sources happened to change again."""
    root = skill_plugin.sync_skill_plugin()
    shutil.rmtree(os.path.join(root, "skills", skill_plugin.SKILLS[0]))

    assert skill_plugin.sync_skill_plugin() == root
    assert os.path.isfile(os.path.join(root, "skills", skill_plugin.SKILLS[0],
                                       "SKILL.md"))


def test_the_manifest_alone_is_not_called_loadable(home, sources):
    root = skill_plugin.plugin_dir()
    os.makedirs(os.path.join(root, ".claude-plugin"))
    open(os.path.join(root, ".claude-plugin", "plugin.json"), "w").close()
    assert skill_plugin._is_loadable(root) is True          # nothing expected
    assert skill_plugin._is_loadable(root, skill_plugin.SKILLS) is False


def test_two_syncs_at_once_do_not_stage_into_the_same_directory(home, sources):
    """`<root>.new` was shared. export_skill_plugin_env is called from the
    create-app and create-template routes, which FastAPI runs on a threadpool, so
    two scaffolds at once could each rmtree the other's half-copied staging and
    publish whichever fragment won."""
    seen = []
    real_build = skill_plugin._build
    # The barrier lives INSIDE the spy so both threads must be inside _build
    # before either finishes: without it, the first sync could publish + stamp
    # before the second even checked loadability, and the second would take the
    # legitimate short-circuit — one _build call, spurious failure. It can't
    # deadlock: the first thread blocked here has published nothing, so the
    # second's stamp check must fail and bring it into _build too.
    barrier = threading.Barrier(2, timeout=30)

    def spy(staging, sources_map):
        seen.append(staging)
        barrier.wait()
        return real_build(staging, sources_map)

    skill_plugin._build = spy
    try:
        def run():
            skill_plugin.sync_skill_plugin()

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        skill_plugin._build = real_build

    assert len(seen) == 2 and seen[0] != seen[1], seen
    # and whoever won, what is published is whole
    root = skill_plugin.plugin_dir()
    assert skill_plugin._is_loadable(root, skill_plugin.SKILLS)


def test_a_staging_directory_is_not_left_behind(home, sources):
    """It lives beside the root, so a leaked one is litter in the user's home
    dir — and mkdtemp names are unique, so they would accumulate."""
    skill_plugin.sync_skill_plugin()
    leftovers = [n for n in os.listdir(home) if ".new-" in n]
    assert leftovers == [], leftovers


# ------------------------------------------------------- the assembled shape

def test_the_sync_builds_a_loadable_plugin_root(home, sources):
    root = skill_plugin.sync_skill_plugin()
    assert root == os.path.join(home, "skill-plugin")
    manifest = os.path.join(root, ".claude-plugin", "plugin.json")
    assert json.load(open(manifest, encoding="utf-8"))["name"] == "fused-render"
    for name in skill_plugin.SKILLS:
        assert os.path.isfile(os.path.join(root, "skills", name, "SKILL.md"))
    # The bookkeeping stamp stays OUT of the tree the plugin loader parses.
    assert os.path.isfile(root + ".stamp.json")
    assert sorted(os.listdir(root)) == [".claude-plugin", "skills"]


def test_a_second_sync_with_unchanged_sources_touches_nothing(home, sources):
    root = skill_plugin.sync_skill_plugin()
    marker = os.path.join(root, "skills", skill_plugin.SKILLS[0], "SKILL.md")
    before = os.stat(marker).st_ino
    assert skill_plugin.sync_skill_plugin() == root
    # Same inode: the stamp short-circuited rather than deleting and recopying
    # the tree under whatever session happens to be reading it.
    assert os.stat(marker).st_ino == before


def test_a_changed_skill_is_picked_up(home, sources):
    root = skill_plugin.sync_skill_plugin()
    src = sources / "skills" / skill_plugin.SKILLS[0] / "SKILL.md"
    src.write_text("# rewritten\n" + "x" * 100, encoding="utf-8")
    skill_plugin.sync_skill_plugin()
    out = os.path.join(root, "skills", skill_plugin.SKILLS[0], "SKILL.md")
    assert "rewritten" in open(out, encoding="utf-8").read()


def test_a_stale_file_does_not_survive_a_rebuild(home, sources):
    """The swap is delete-then-rename, not a merge: a skill (or a file inside
    one) that the sources no longer have must not linger in the output, or a
    session keeps loading guidance the install stopped shipping."""
    root = skill_plugin.sync_skill_plugin()
    junk = os.path.join(root, "skills", skill_plugin.SKILLS[0], "OLD.md")
    open(junk, "w").close()
    (sources / "skills" / skill_plugin.SKILLS[0] / "SKILL.md").write_text(
        "# changed\n", encoding="utf-8")
    skill_plugin.sync_skill_plugin()
    assert not os.path.exists(junk)


def test_the_packaged_copy_is_the_fallback(home, tmp_path, monkeypatch):
    """A wheel install has no repo. The packaged manifest is FLAT
    (skills/plugin.json) and still lands as .claude-plugin/plugin.json."""
    packaged = tmp_path / "pkg" / "skills"
    for name in skill_plugin.SKILLS:
        (packaged / name).mkdir(parents=True)
        (packaged / name / "SKILL.md").write_text("# s\n", encoding="utf-8")
    (packaged / "plugin.json").write_text(
        json.dumps({"name": "fused-render"}), encoding="utf-8")
    monkeypatch.setattr(skill_plugin, "_REPO_SKILLS_DIR", str(tmp_path / "gone"))
    monkeypatch.setattr(skill_plugin, "_REPO_MANIFEST", str(tmp_path / "gone.json"))
    monkeypatch.setattr(skill_plugin, "_PACKAGED_SKILLS_DIR", str(packaged))
    monkeypatch.setattr(skill_plugin, "_PACKAGED_MANIFEST",
                        str(packaged / "plugin.json"))
    root = skill_plugin.sync_skill_plugin()
    assert json.load(open(os.path.join(root, ".claude-plugin", "plugin.json"),
                          encoding="utf-8"))["name"] == "fused-render"
    for name in skill_plugin.SKILLS:
        assert os.path.isfile(os.path.join(root, "skills", name, "SKILL.md"))
    # `plugin.json` is a file at the skills root, not a skill: it must not be
    # copied in as one.
    assert not os.path.exists(os.path.join(root, "skills", "plugin.json"))


def test_no_source_at_all_is_not_an_error(home, tmp_path, monkeypatch):
    """Callers are server startup and scaffolding; neither may fail over this."""
    for attr in ("_REPO_SKILLS_DIR", "_PACKAGED_SKILLS_DIR"):
        monkeypatch.setattr(skill_plugin, attr, str(tmp_path / "gone"))
    for attr in ("_REPO_MANIFEST", "_PACKAGED_MANIFEST"):
        monkeypatch.setattr(skill_plugin, attr, str(tmp_path / "gone.json"))
    assert skill_plugin.sync_skill_plugin() is None


def test_a_missing_manifest_still_yields_a_loadable_plugin(home, sources):
    """No manifest means the CLI ignores the root and the skills go with it, so
    a synthesized minimum beats shipping nothing — the failure is logged, not
    silent."""
    os.remove(sources / ".claude-plugin" / "plugin.json")
    root = skill_plugin.sync_skill_plugin()
    data = json.load(open(os.path.join(root, ".claude-plugin", "plugin.json"),
                          encoding="utf-8"))
    assert data["name"] == "fused-render"


def test_an_unwritable_home_returns_none_rather_than_raising(home, sources,
                                                            monkeypatch):
    def boom(*a, **kw):
        raise OSError("nope")

    monkeypatch.setattr(skill_plugin.shutil, "copytree", boom)
    assert skill_plugin.sync_skill_plugin() is None


# --------------------------------------------------- the packaging invariant

def test_nothing_the_wheel_ships_lives_under_a_dotted_path():
    """The reason the packaged manifest is `fused_render/skills/plugin.json` and
    not `fused_render/skills/.claude-plugin/plugin.json`: whether a hidden path
    survives the build backend's `artifacts` globs is exactly the sort of thing
    that fails only in a built wheel, with no error anywhere. So the packaged
    source tree has no dotted entries at all, and the dotted dir is created by
    the sync instead."""
    assert skill_plugin._PACKAGED_MANIFEST == os.path.join(
        skill_plugin._PACKAGED_SKILLS_DIR, "plugin.json")
    for path in (skill_plugin._PACKAGED_SKILLS_DIR, skill_plugin._PACKAGED_MANIFEST):
        rel = os.path.relpath(path, skill_plugin._REPO_ROOT)
        assert not rel.startswith(os.pardir), rel
        assert not any(part.startswith(".") for part in rel.split(os.sep)), rel


def test_the_build_hook_copies_the_manifest_where_the_fallback_looks():
    """hatch_build's `_copy_starter_skills` writes the packaged manifest; the two
    halves of that contract live in different files and only agree by
    convention, so pin the convention."""
    src = open(os.path.join(REPO_ROOT, "scripts", "hatch_build.py"),
               encoding="utf-8").read()
    assert '".claude-plugin", "plugin.json"' in src
    assert 'os.path.join(dest_root, "plugin.json")' in src


def test_the_packaged_tree_is_listed_as_a_wheel_artifact():
    """`fused_render/skills/` is gitignored (a build-time copy), so hatchling
    excludes it from the wheel unless `artifacts` says otherwise — and a wheel
    without it has no source for either delivery on a machine with no repo,
    which is every end user."""
    text = open(os.path.join(REPO_ROOT, "pyproject.toml"), encoding="utf-8").read()
    assert '"fused_render/skills/**"' in text


def test_the_repo_root_is_itself_a_plugin_root():
    """The committed `.claude-plugin/plugin.json` + `skills/` is what makes
    `claude plugin marketplace add fusedio/fused-render` work, and it is the
    source the dev/editable path reads. Both consumers break if either half
    moves."""
    manifest = os.path.join(REPO_ROOT, ".claude-plugin", "plugin.json")
    assert json.load(open(manifest, encoding="utf-8"))["name"] == "fused-render"
    for name in skill_plugin.SKILLS:
        assert os.path.isfile(
            os.path.join(REPO_ROOT, "skills", name, "SKILL.md"))


# ------------------------------------------------------- the two duplications

def test_the_two_skill_deliveries_agree_on_the_skill_list():
    """D216's plugin root and D185's user-level sync ship the same three skills
    from the same two source roots. Separate modules, deliberately — but a skill
    added to one list and not the other would be missing from half the sessions
    on the machine."""
    assert skill_plugin.SKILLS == user_skills.SKILLS
    assert skill_plugin._REPO_SKILLS_DIR == user_skills._REPO_SKILLS_DIR
    assert skill_plugin._PACKAGED_SKILLS_DIR == user_skills._PACKAGED_SKILLS_DIR


def _agent(template):
    path = os.path.join(REPO_ROOT, "fused_render", "templates", template,
                        "agent.py")
    spec = importlib.util.spec_from_file_location("_agent_" + template, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(params=["claude"])
def agent(request):
    """The chat template's agent. Parametrised over both chat templates while a
    plain chat and a split chat existed as forks of each other, because the
    `--plugin-dir` wiring had to stay in lockstep across them; the plain one is
    deleted, so there is one. Left as a params list — that is the seam a second
    agent would re-enter through, and collapsing it would rewrite every test
    signature for no behavioural gain."""
    return _agent(request.param)


def _spawn_argv(agent, tmp_path, monkeypatch):
    """`_start` against a fake Popen, returning the argv it built."""
    target = tmp_path / "page.html"
    target.write_text("<p>hi</p>", encoding="utf-8")
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))
    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    seen = {}
    monkeypatch.setattr(
        agent.subprocess, "Popen",
        lambda cmd, **kw: (seen.__setitem__("cmd", cmd),
                           type("P", (), {"pid": 4242})())[1])
    out = agent._start(str(target), "hi", "", "", "")
    assert "error" not in out, out
    return seen["cmd"]


def test_a_spawned_session_is_handed_the_plugin(agent, tmp_path, monkeypatch):
    """The whole point of D216: a session fused-render launches gets the skills
    from a path we just wrote, not from whatever state the user's ~/.claude
    happens to be in."""
    monkeypatch.setenv(skill_plugin.PLUGIN_DIR_ENV, "/somewhere/skill-plugin")
    cmd = _spawn_argv(agent, tmp_path, monkeypatch)
    assert cmd[cmd.index("--plugin-dir") + 1] == "/somewhere/skill-plugin"


def test_an_unpublished_root_means_no_flag(agent, tmp_path, monkeypatch):
    """No var is the state before any server started and after a sync that
    failed — both must spawn a plain turn. `_start` does NOT re-derive or
    re-check any of that: it would mean a subprocess per turn, and every one of
    these tests fakes Popen."""
    monkeypatch.delenv(skill_plugin.PLUGIN_DIR_ENV, raising=False)
    assert "--plugin-dir" not in _spawn_argv(agent, tmp_path, monkeypatch)


def test_the_spawn_path_never_shells_out_to_the_cli(agent, tmp_path, monkeypatch):
    """A regression guard with a scar behind it: probing `claude --help` inside
    `_start` broke a dozen existing tests, because `subprocess.run` goes through
    the `Popen` they all fake — and in production it would have added a
    subprocess to every turn. The decision belongs to the server."""
    monkeypatch.setenv(skill_plugin.PLUGIN_DIR_ENV, "/somewhere/skill-plugin")

    def no_run(*a, **kw):
        raise AssertionError("_start must not run a subprocess of its own")

    monkeypatch.setattr(agent.subprocess, "run", no_run)
    assert "--plugin-dir" in _spawn_argv(agent, tmp_path, monkeypatch)


# ------------------------------------------- publishing the root to a session

def test_the_export_publishes_the_root(home, sources):
    root = skill_plugin.export_skill_plugin_env()
    assert root == skill_plugin.plugin_dir()
    assert os.environ[skill_plugin.PLUGIN_DIR_ENV] == root


def test_a_failed_sync_clears_a_stale_publication(home, tmp_path, monkeypatch):
    """The var is inherited by every child, so leaving a path there that no
    longer holds a plugin would make each spawn pass `--plugin-dir` at nothing."""
    monkeypatch.setenv(skill_plugin.PLUGIN_DIR_ENV, "/stale")
    for attr in ("_REPO_SKILLS_DIR", "_PACKAGED_SKILLS_DIR"):
        monkeypatch.setattr(skill_plugin, attr, str(tmp_path / "gone"))
    for attr in ("_REPO_MANIFEST", "_PACKAGED_MANIFEST"):
        monkeypatch.setattr(skill_plugin, attr, str(tmp_path / "gone.json"))
    assert skill_plugin.export_skill_plugin_env() is None
    assert skill_plugin.PLUGIN_DIR_ENV not in os.environ


def test_the_export_never_spawns_a_subprocess(home, sources, monkeypatch):
    """The scar. `export_app_env` runs this BEFORE uvicorn binds its socket, so
    anything slow here delays the bind — and the desktop supervisor gives the
    whole child 20s (`supervisor/core.py:_READY_TIMEOUT_S`) before killing it and
    retrying. A `claude --help` probe used to live here; on a cold 279MB binary
    behind a Windows Defender first-touch scan it answered in ~53s, so the
    supervisor killed a healthy server three times and showed a startup-failure
    dialog. Keep this path filesystem-only."""
    def no_spawn(*a, **kw):
        raise AssertionError("export_skill_plugin_env must not run a subprocess")

    for name in ("run", "Popen", "check_output", "call", "check_call"):
        monkeypatch.setattr(subprocess, name, no_spawn)
    assert skill_plugin.export_skill_plugin_env() == skill_plugin.plugin_dir()


def test_the_real_cli_still_takes_the_flag():
    """The one assertion here about the OUTSIDE world, and the only thing now
    standing behind the assumption that replaced the probe: we pass
    `--plugin-dir` unconditionally, and an unknown option makes the CLI exit
    before the turn starts. If it is ever renamed or dropped, every spawned chat
    dies — so fail here, loudly, rather than in a user's session.

    `--plugin-dir` has shipped since the plugin system itself (CLI 2.0.12,
    Oct 2025) on a binary that auto-updates, which is why the runtime no longer
    checks. This test is where that bet gets re-examined."""
    binary = shutil.which("claude")
    if binary is None:
        pytest.skip("no claude on PATH")
    out = subprocess.run([binary, "--help"], capture_output=True, text=True,
                         timeout=120)
    assert "--plugin-dir" in ((out.stdout or "") + (out.stderr or ""))


@pytest.mark.parametrize("template", ["claude"])
def test_a_template_reads_the_root_through_appenv_only(template):
    """The chat template passes `--plugin-dir`, and may not import
    `fused_render` (SPEC PY-15). It also must not REBUILD the path from
    `home_dir()`: `home_dir()` nests under `branches/<ref>/` on a branch build
    and only the server knows the resolved answer, so re-deriving is how a build
    syncs one dir and loads another — i.e. loads nothing. The one legal route is
    the appenv reader, which returns the server's already-made decision."""
    src = open(os.path.join(REPO_ROOT, "fused_render", "templates", template,
                            "agent.py"), encoding="utf-8").read()
    assert "from appenv import skill_plugin_dir as _skill_plugin_dir" in src
    # No second spelling of the subdir name, and no home-dir arithmetic for it.
    assert skill_plugin.PLUGIN_SUBDIR not in src


def test_appenv_names_the_var_the_server_exports():
    """The two ends of the env contract, in files that never import each other."""
    appenv = open(os.path.join(REPO_ROOT, "fused_render", "templates", "shared",
                               "appenv.py"), encoding="utf-8").read()
    assert skill_plugin.PLUGIN_DIR_ENV in appenv


# -- the WORKBENCH skills: an app-owned clone, handed to CANVAS sessions only ---
#
# A canvas clone's CLAUDE.md names `workbench:canvas-toml` and friends. Those
# skills are not in this wheel: they live in the public `fusedio/skills` repo,
# which the app shallow-clones into a directory it owns under `home_dir()` and
# refreshes at most once per interval. Two properties matter, and they fail in
# different places:
#
# * the ROOT — only `<clone>/workbench` with every skill in WORKBENCH_SKILLS
#   present is ever offered. A half-fetched or stubbed tree must read as
#   "nothing to hand" (it would load cleanly and teach the model nothing), and a
#   failed fetch must leave a previously-good clone exactly where it was.
# * the GATE — the root goes to canvas-clone sessions only. Handing it to every
#   session leaks canvas/UDF guidance into file, app-folder and plain-folder
#   chats that have no canvas anywhere near them.
#
# Machine discovery (a scan of Claude Code's own
# plugins/{marketplaces,cache}/<market>/workbench) used to answer the first half
# and is deleted: the layout is private, so the scan could not tell an installed
# plugin from an UNINSTALLED one — the uninstall leaves a tombstoned
# cache/<market>/workbench/<sha>/ dir behind and the scan resurrected it. The
# tests below therefore assert the app's OWN clone dir, and nothing here may
# reach into `~/.claude` again.


def _load_agent():
    """The claude template's agent.py as a module (it is not importable as part
    of the package — SPEC PY-15 — so it is loaded from its path, the same way
    every other test of it does)."""
    path = os.path.join(REPO_ROOT, "fused_render", "templates", "claude", "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent_for_plugins", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _plugin_tree(root, skills, name="workbench"):
    """A minimal but LOADABLE plugin root."""
    os.makedirs(os.path.join(root, skill_plugin.MANIFEST_DIR), exist_ok=True)
    with open(os.path.join(root, skill_plugin.MANIFEST_DIR,
                           skill_plugin.MANIFEST_NAME), "w", encoding="utf-8") as fh:
        json.dump({"name": name}, fh)
    for skill in skills:
        d = os.path.join(root, skill_plugin.SKILLS_SUBDIR, skill)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("# %s\n" % skill)
    return root


def _clone_tree(home, skills=skill_plugin.WORKBENCH_SKILLS, git="dir"):
    """The clone as a successful fetch leaves it: `<home>/workbench-skills/`
    holding a `workbench/` plugin root.

    `git` shapes the `.git` entry — "dir" (a plain clone), "file" (a worktree or
    submodule checkout, which is just as valid a repo) or None. The fetch must
    not care: what decides refresh-vs-clone is whether the root is LOADABLE.
    """
    clone = os.path.join(home, skill_plugin.WORKBENCH_CLONE_SUBDIR)
    os.makedirs(clone, exist_ok=True)
    if git == "dir":
        os.makedirs(os.path.join(clone, ".git"), exist_ok=True)
    elif git == "file":
        with open(os.path.join(clone, ".git"), "w", encoding="utf-8") as fh:
            fh.write("gitdir: /somewhere/else/.git/worktrees/skills\n")
    return _plugin_tree(os.path.join(clone, skill_plugin.WORKBENCH_PLUGIN_SUBDIR),
                        skills)


@pytest.fixture
def no_override(monkeypatch):
    monkeypatch.delenv(skill_plugin.WORKBENCH_PLUGIN_SRC_ENV, raising=False)


@pytest.fixture
def fake_git(monkeypatch):
    """The git seam, recording every invocation. `clone` materializes a real
    plugin tree at the destination so the swap-and-validate path runs for
    real; anything else just succeeds."""
    calls = []

    def run(args, timeout):
        calls.append(list(args))
        if args[0] == "clone":
            _plugin_tree(os.path.join(args[-1], skill_plugin.WORKBENCH_PLUGIN_SUBDIR),
                         skill_plugin.WORKBENCH_SKILLS)
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(skill_plugin, "_git", run)
    return calls


def test_no_clone_is_a_normal_outcome(home, no_override):
    assert skill_plugin.workbench_plugin_root() is None
    assert skill_plugin.export_workbench_plugin_env() is None
    assert skill_plugin.WORKBENCH_PLUGIN_DIR_ENV not in os.environ


def test_a_valid_clone_is_the_root_and_the_export_publishes_it(home, no_override):
    root = _clone_tree(home)
    assert skill_plugin.workbench_plugin_root() == root
    assert skill_plugin.export_workbench_plugin_env() == root
    assert os.environ[skill_plugin.WORKBENCH_PLUGIN_DIR_ENV] == root
    # Clone gone (a wiped home dir, a failed swap) → the var must go with it, or
    # every later session is handed a --plugin-dir pointing at nothing.
    shutil.rmtree(root)
    assert skill_plugin.export_workbench_plugin_env() is None
    assert skill_plugin.WORKBENCH_PLUGIN_DIR_ENV not in os.environ


def test_a_gutted_clone_is_not_offered(home, no_override):
    """The manifest alone is not evidence: a root missing the very skills the
    CLAUDE.md names would load cleanly and teach the model nothing — exactly the
    silent failure this mechanism exists to remove."""
    _clone_tree(home, ["canvas-toml"])  # manifest + one skill, not the set
    assert skill_plugin.workbench_plugin_root() is None


def test_the_startup_export_runs_no_subprocess(home, no_override, monkeypatch):
    """The export is on the PRE-BIND startup path, where blocking is a server
    the desktop supervisor kills — so it publishes an existing clone and never
    fetches one. The fetch belongs to the canvases path."""
    def no_spawn(*a, **kw):
        raise AssertionError("the startup workbench export must not spawn")

    for name in ("run", "Popen", "check_output", "call", "check_call"):
        monkeypatch.setattr(subprocess, name, no_spawn)
    assert skill_plugin.export_workbench_plugin_env() is None


def test_the_fetch_shallow_clones_over_https(home, no_override, fake_git):
    root = skill_plugin.fetch_workbench_skills()
    assert root == os.path.join(home, skill_plugin.WORKBENCH_CLONE_SUBDIR,
                                skill_plugin.WORKBENCH_PLUGIN_SUBDIR)
    assert os.path.isdir(root)
    assert len(fake_git) == 1
    argv = fake_git[0]
    assert argv[:3] == ["clone", "--depth", "1"]
    assert skill_plugin.WORKBENCH_REPO_URL in argv
    # HTTPS, never SSH: a shipping user has no SSH key, so an ssh:// remote
    # would fail on exactly the machines this exists for.
    assert skill_plugin.WORKBENCH_REPO_URL.startswith("https://")


def test_a_failed_clone_publishes_nothing(home, no_override, monkeypatch):
    """Staging exists for this: a failed clone must not leave a half-tree at the
    real path for the next session to load."""
    def run(args, timeout):
        return subprocess.CompletedProcess(list(args), 128, "", "fatal: no network")

    monkeypatch.setattr(skill_plugin, "_git", run)
    assert skill_plugin.fetch_workbench_skills() is None
    assert not os.path.exists(skill_plugin.workbench_clone_dir())


def test_a_failed_refresh_leaves_the_good_clone_alone(home, no_override, monkeypatch):
    root = _clone_tree(home)
    calls = []

    def run(args, timeout):
        calls.append(list(args))
        return subprocess.CompletedProcess(list(args), 1, "", "fatal: no network")

    monkeypatch.setattr(skill_plugin, "_git", run)
    assert skill_plugin.fetch_workbench_skills() == root
    assert os.path.isfile(os.path.join(root, skill_plugin.SKILLS_SUBDIR,
                                       "canvas-toml", "SKILL.md"))
    # It tried to refresh (no stamp yet) and stopped at the failed fetch rather
    # than resetting a tree it could not update.
    assert [c[2] for c in calls] == ["fetch"]


def test_the_refresh_is_rate_limited(home, no_override, fake_git):
    """Hours, not minutes: this runs on a user-visible request, and a needless
    network round trip there is a stall the user sees."""
    _clone_tree(home)
    skill_plugin.fetch_workbench_skills()
    assert [c[2] for c in fake_git] == ["fetch", "reset"]
    fake_git.clear()
    # Second call inside the interval: nothing at all.
    skill_plugin.fetch_workbench_skills()
    assert fake_git == []
    # Stamp aged past the interval → one more refresh.
    old = time.time() - skill_plugin.WORKBENCH_REFRESH_S - 1
    with open(skill_plugin._stamp_file(), "w", encoding="utf-8") as fh:
        fh.write("%d" % int(old))
    skill_plugin.fetch_workbench_skills()
    assert [c[2] for c in fake_git] == ["fetch", "reset"]


def test_a_failed_first_clone_is_retried_soon_not_in_six_hours(
        home, no_override, monkeypatch):
    """The rate limit is right for a REFRESH and wrong when nothing is published.

    Sharing one interval put the new-user path — first canvas ever, one
    transient network failure — six hours away from working: the stamp was
    written, the next POST /api/canvases/clone made no attempt at all, and the
    session was handed no `--plugin-dir` while the CLAUDE.md seeded beside it
    named `workbench:canvas-toml` and friends. Skipping a refresh keeps a
    working plugin; skipping a retry leaves none.
    """
    calls = []

    def failing(args, timeout):
        calls.append(list(args))
        return subprocess.CompletedProcess(list(args), 128, "", "fatal: no network")

    monkeypatch.setattr(skill_plugin, "_git", failing)
    assert skill_plugin.fetch_workbench_skills() is None
    assert len(calls) == 1

    # Nothing published → the next canvas clone tries again, on the RETRY
    # interval, not the refresh one.
    monkeypatch.setattr(skill_plugin, "WORKBENCH_RETRY_S", 0)
    assert skill_plugin.fetch_workbench_skills() is None
    assert len(calls) == 2
    # Still bounded: inside the retry window it does not spawn at all, so an
    # offline machine costs one attempt per interval, not one per clone.
    monkeypatch.setattr(skill_plugin, "WORKBENCH_RETRY_S", 300)
    assert skill_plugin.fetch_workbench_skills() is None
    assert len(calls) == 2
    # The retry interval must stay far below the refresh interval, or this
    # regresses back into the six-hour hole.
    assert skill_plugin.WORKBENCH_RETRY_S <= skill_plugin.WORKBENCH_REFRESH_S / 10


def test_once_published_the_slow_refresh_interval_takes_over(
        home, no_override, monkeypatch, fake_git):
    """The retry interval applies only while there is nothing to hand over."""
    monkeypatch.setattr(skill_plugin, "WORKBENCH_RETRY_S", 0)
    assert skill_plugin.fetch_workbench_skills() is not None
    assert [c[0] for c in fake_git] == ["clone"]
    fake_git.clear()
    # A root exists now, so the 6h refresh interval governs — even though the
    # retry interval is zero.
    assert skill_plugin.fetch_workbench_skills() is not None
    assert fake_git == []


def test_a_loadable_clone_is_never_deleted_over_a_git_entry_shape(
        home, no_override, monkeypatch):
    """The clone path's `rmtree` is only safe under "nothing loadable is there".

    Deciding refresh-vs-clone on a `.git` DIRECTORY would delete a perfectly
    good tree whose `.git` is a FILE — the normal layout for a worktree or
    submodule checkout — and re-clone it from scratch, or, if the network were
    down, leave the user with nothing where a working plugin had been.
    """
    for shape in ("file", None):
        shutil.rmtree(skill_plugin.workbench_clone_dir(), ignore_errors=True)
        root = _clone_tree(home, git=shape)
        calls = []

        def run(args, timeout):
            calls.append(list(args))
            return subprocess.CompletedProcess(list(args), 1, "", "not a repository")

        monkeypatch.setattr(skill_plugin, "_git", run)
        assert skill_plugin.fetch_workbench_skills() == root, shape
        assert os.path.isfile(os.path.join(root, skill_plugin.SKILLS_SUBDIR,
                                           "canvas-toml", "SKILL.md")), shape
        assert [c[2] for c in calls] == ["fetch"], (shape, calls)
        os.remove(skill_plugin._stamp_file())


def test_the_refresh_hard_resets_to_origins_default_branch(home, no_override, fake_git):
    """A dirty or diverged clone has to self-heal — and the branch NAME is the
    repo's business, so the fetch names HEAD rather than `main`."""
    _clone_tree(home)
    skill_plugin.fetch_workbench_skills()
    fetch, reset = fake_git
    assert fetch == ["-C", skill_plugin.workbench_clone_dir(), "fetch", "--depth",
                     "1", "origin", "HEAD"]
    assert reset == ["-C", skill_plugin.workbench_clone_dir(), "reset", "--hard",
                     "FETCH_HEAD"]


def test_no_git_on_the_machine_is_a_normal_outcome(home, no_override, monkeypatch):
    def missing(args, timeout):
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    monkeypatch.setattr(skill_plugin, "_git", missing)
    assert skill_plugin.fetch_workbench_skills() is None
    assert skill_plugin.sync_workbench_plugin() is None
    assert skill_plugin.WORKBENCH_PLUGIN_DIR_ENV not in os.environ


def test_a_timeout_is_a_normal_outcome(home, no_override, monkeypatch):
    def hang(args, timeout):
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr(skill_plugin, "_git", hang)
    assert skill_plugin.fetch_workbench_skills() is None


def test_the_git_spawn_keeps_the_posix_spawn_discipline():
    """close_fds=False and an ABSOLUTE git path, never `cwd=`: a fork in this
    process runs PROJ's atfork handler into a SIGSEGV before exec, so `git`
    dies rc=-11 with no output at all. Same discipline as app_git.py."""
    src = open(os.path.join(REPO_ROOT, "fused_render", "skill_plugin.py"),
               encoding="utf-8").read()
    body = src[src.index("def _git("):src.index("def workbench_clone_dir")]
    body = body[body.index("return subprocess.run"):]  # the call, not its prose
    assert "close_fds=False" in body
    assert "cwd=" not in body
    assert "_git_bin()" in body
    assert "shutil.which(\"git\")" in src


def test_the_override_bypasses_all_fetching(tmp_path, home, monkeypatch):
    def no_git(args, timeout):
        raise AssertionError("the dev override must never be fetched into")

    monkeypatch.setattr(skill_plugin, "_git", no_git)
    mine = _plugin_tree(tmp_path / "mine", skill_plugin.WORKBENCH_SKILLS)
    monkeypatch.setenv(skill_plugin.WORKBENCH_PLUGIN_SRC_ENV, str(mine))
    assert skill_plugin.workbench_plugin_root() == str(mine)
    assert skill_plugin.fetch_workbench_skills() == str(mine)
    assert skill_plugin.sync_workbench_plugin() == str(mine)
    # An override pointing at nothing does not silently fall back to a clone.
    _clone_tree(home)
    monkeypatch.setenv(skill_plugin.WORKBENCH_PLUGIN_SRC_ENV, str(tmp_path / "nope"))
    assert skill_plugin.workbench_plugin_root() is None
    assert skill_plugin.fetch_workbench_skills() is None


def test_no_machine_discovery_is_left_anywhere(home, monkeypatch, tmp_path):
    """The scan of Claude Code's private plugin storage is gone, and must not
    come back: it resurrected a plugin the user had UNINSTALLED (the uninstall
    leaves the cache dir behind, tombstoned with `.orphaned_at`)."""
    for gone in ("find_workbench_plugin", "_workbench_candidates",
                 "WORKBENCH_PLUGIN_NAME"):
        assert not hasattr(skill_plugin, gone), gone
    src = open(os.path.join(REPO_ROOT, "fused_render", "skill_plugin.py"),
               encoding="utf-8").read()
    body = src[src.index("# -- the WORKBENCH"):]
    # Comments deliberately still explain WHY the scan is gone; the code must
    # not name any of it.
    code = "\n".join(ln for ln in body.split("\n")
                     if not ln.lstrip().startswith("#"))
    for needle in ("marketplaces", "CLAUDE_CONFIG_DIR", "orphaned", "plugins"):
        assert needle not in code, needle
    # And behaviourally: a plugin sitting in Claude Code's storage is not found.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    monkeypatch.delenv(skill_plugin.WORKBENCH_PLUGIN_SRC_ENV, raising=False)
    _plugin_tree(tmp_path / "cc" / "plugins" / "marketplaces" / "m" / "workbench",
                 skill_plugin.WORKBENCH_SKILLS)
    assert skill_plugin.workbench_plugin_root() is None


def test_the_claude_template_passes_the_workbench_root_to_canvases_only(
        tmp_path, monkeypatch):
    """The gate. `--plugin-dir` is repeatable, which is what lets the two
    plugins compose without merging trees — but the workbench root is for canvas
    clones alone, so the target decides whether it is passed at all."""
    canvases = tmp_path / "canvases"
    (canvases / "my_canvas").mkdir(parents=True)
    agent = _load_agent()
    monkeypatch.setattr(agent, "_skill_plugin_dir", lambda: "/a/own")
    monkeypatch.setattr(agent, "_workbench_plugin_dir", lambda: "/b/workbench")
    monkeypatch.setattr(agent, "_canvases_root", lambda: str(canvases))
    both = ["--plugin-dir", "/a/own", "--plugin-dir", "/b/workbench"]
    own = ["--plugin-dir", "/a/own"]

    inside = str(canvases / "my_canvas" / "canvas.toml")
    assert agent._plugin_argv(inside) == both
    assert agent._plugin_argv(str(canvases / "my_canvas")) == both
    # A sibling whose name merely STARTS with the root's characters is a
    # different directory — a string prefix check would hand it the skills.
    assert agent._plugin_argv(str(tmp_path / "canvases-evil" / "x.py")) == own
    assert agent._plugin_argv(str(tmp_path / "elsewhere" / "app.html")) == own
    assert agent._plugin_argv(None) == own
    # Either root can still be absent independently.
    monkeypatch.setattr(agent, "_workbench_plugin_dir", lambda: None)
    assert agent._plugin_argv(inside) == own
    monkeypatch.setattr(agent, "_skill_plugin_dir", lambda: None)
    assert agent._plugin_argv(inside) == []
    monkeypatch.setattr(agent, "_workbench_plugin_dir", lambda: "/b/workbench")
    assert agent._plugin_argv(inside) == ["--plugin-dir", "/b/workbench"]
    assert agent._plugin_argv(str(tmp_path / "elsewhere")) == []


def test_both_callers_pass_their_target_to_the_gate():
    """A caller that forgets the argument silently withholds the skills from
    every canvas session — the gate defaults to "no", so the failure is quiet."""
    src = open(os.path.join(REPO_ROOT, "fused_render", "templates", "claude",
                            "agent.py"), encoding="utf-8").read()
    assert "_plugin_argv()" not in src
    assert src.count("_plugin_argv(file)") == 2


def test_appenv_names_the_workbench_var_and_the_canvases_root():
    appenv = open(os.path.join(REPO_ROOT, "fused_render", "templates", "shared",
                               "appenv.py"), encoding="utf-8").read()
    assert skill_plugin.WORKBENCH_PLUGIN_DIR_ENV in appenv
    assert "FUSED_RENDER_CANVASES_DIR" in appenv
    src = open(os.path.join(REPO_ROOT, "fused_render", "templates", "claude",
                            "agent.py"), encoding="utf-8").read()
    assert "from appenv import workbench_plugin_dir as _workbench_plugin_dir" in src
    assert "from appenv import canvases_root as _canvases_root" in src


def _load_appenv():
    path = os.path.join(REPO_ROOT, "fused_render", "templates", "shared", "appenv.py")
    spec = importlib.util.spec_from_file_location("appenv_for_canvases", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_appenv_canvases_root_matches_the_servers_own(monkeypatch, tmp_path):
    """The two copies of the rule (SPEC PY-15 forbids the import) must agree in
    BOTH branches, asserted against each other rather than against a spelled-out
    expectation — a disagreement makes `_in_canvases_root` answer "not a canvas"
    for a real clone, and the gate's default is to withhold the skills, so the
    failure is silent.

    The third participant is `_canvas_push.canvases_root()`, the shim the
    intercepted `fused workbench canvas push` runs through; all three resolve the
    same folder or the feature breaks in a different place each time.
    """
    import fused_render.canvases as canvases_mod
    from fused_render import _canvas_push

    appenv = _load_appenv()

    # 1. Exported (the production path): all three read the var.
    monkeypatch.setenv("FUSED_RENDER_CANVASES_DIR", str(tmp_path / "cans"))
    assert appenv.canvases_root() == str(tmp_path / "cans")
    assert canvases_mod.canvases_root() == appenv.canvases_root()
    assert _canvas_push.canvases_root() == appenv.canvases_root()

    # 2. Unexported (a standalone template, or a server whose export regressed):
    # the fallbacks must still land on the same folder. Deliberately with
    # FUSED_RENDER_HOME/HOME_DIR pointed elsewhere, because that is exactly the
    # case that used to diverge: appenv resolved through home_dir() (branch
    # nesting included) while the server hardcodes ~/.fused-render/canvases.
    monkeypatch.delenv("FUSED_RENDER_CANVASES_DIR")
    monkeypatch.setenv("FUSED_RENDER_HOME_DIR", str(tmp_path / "branchhome"))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "otherhome"))
    assert appenv.canvases_root() == canvases_mod.canvases_root()
    assert appenv.canvases_root() == _canvas_push.canvases_root()
    assert appenv.canvases_root() == os.path.expanduser("~/.fused-render/canvases")


def test_the_gate_normalizes_before_comparing(tmp_path, monkeypatch):
    """Every normalization step guards a silent withholding of the skills.

    A relative target (`_terminal_command` does not abspath its `file`), a
    `/tmp` vs `/private/tmp` symlink, or — on Windows — a path differing only in
    case are all the SAME canvas clone, and the gate answering "no" for any of
    them hands the session no workbench skills while its CLAUDE.md names them.
    """
    canvases = tmp_path / "canvases"
    (canvases / "c1").mkdir(parents=True)
    agent = _load_agent()
    monkeypatch.setattr(agent, "_skill_plugin_dir", lambda: "/a/own")
    monkeypatch.setattr(agent, "_workbench_plugin_dir", lambda: "/b/workbench")
    monkeypatch.setattr(agent, "_canvases_root", lambda: str(canvases))

    assert agent._in_canvases_root(str(canvases / "c1" / "canvas.toml"))
    # Relative to the process cwd — resolved, not compared as typed.
    monkeypatch.chdir(canvases / "c1")
    assert agent._in_canvases_root("canvas.toml")
    assert agent._in_canvases_root(os.path.join(".", "sub", "x.py"))
    monkeypatch.chdir(tmp_path)
    assert not agent._in_canvases_root("canvas.toml")
    # Case-insensitive only where the platform is: normcase is a no-op on POSIX,
    # so this asserts the *rule*, not a case-folded answer on macOS/Linux.
    upper = str(canvases / "c1").upper()
    assert agent._in_canvases_root(upper) == (
        os.path.normcase(upper) == os.path.normcase(str(canvases / "c1")))
    # And the containment check itself still refuses a sibling by name.
    assert not agent._in_canvases_root(str(tmp_path / "canvases-evil" / "x.py"))


def test_the_server_exports_the_root_before_serving():
    """The vars have to be set before any child is spawned, or a session
    inherits nothing — same contract as every other FUSED_RENDER_* export."""
    src = open(os.path.join(REPO_ROOT, "fused_render", "server", "app.py"),
               encoding="utf-8").read()
    export = src[src.index("def export_app_env"):]
    assert "export_workbench_plugin_env()" in export
    assert "FUSED_RENDER_CANVASES_DIR" in export


def test_both_canvas_entry_points_fetch_and_neither_can_fail_over_it():
    """Startup may not fetch (pre-bind), so the canvas paths must — and BOTH of
    them, which is the field bug: hanging it on /clone alone meant a canvas that
    already existed never got the skills, and a session that cannot find the
    skills its own CLAUDE.md names goes hunting for the format across the
    filesystem. The kick is off-thread and swallows everything, so neither route
    can fail or stall over a skill fetch."""
    src = open(os.path.join(REPO_ROOT, "fused_render", "canvases.py"),
               encoding="utf-8").read()
    for entry in ("def api_canvases_clone", "def api_canvases_sync_start"):
        route = src[src.index(entry):]
        route = route[:route.index("\n@router")]
        assert "_kick_workbench_skills()" in route, entry
    helper = src[src.index("def _kick_workbench_skills"):]
    helper = helper[:helper.index("\ndef ")]
    assert "sync_workbench_plugin" in helper
    assert "except Exception" in helper
    assert "daemon=True" in helper
    assert "acquire(blocking=False)" in helper
