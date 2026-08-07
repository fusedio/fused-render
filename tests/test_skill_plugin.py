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
import threading

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


@pytest.fixture(params=["claude", "claude_split"])
def agent(request):
    """Both claude templates, which are deliberate forks of each other — the
    `--plugin-dir` wiring is one of the things that has to stay in lockstep
    across them."""
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
    """No var is the state before any server started, a sync that failed, and a
    CLI that cannot take the flag — all three must spawn a plain turn. `_start`
    does NOT re-derive or re-check any of that: it would mean a subprocess per
    turn, and every one of these tests fakes Popen."""
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

def test_the_export_publishes_the_root(home, sources, monkeypatch):
    monkeypatch.setattr(skill_plugin, "_claude_bin", lambda: None)
    root = skill_plugin.export_skill_plugin_env()
    assert root == skill_plugin.plugin_dir()
    assert os.environ[skill_plugin.PLUGIN_DIR_ENV] == root


def test_a_cli_that_cannot_take_the_flag_is_not_given_it(home, sources,
                                                         monkeypatch):
    """Fail CLOSED. An unknown option makes the CLI exit before the turn starts,
    so an older install loses the skills rather than every chat."""
    monkeypatch.setattr(skill_plugin, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(skill_plugin, "_supports_plugin_dir", lambda _b: False)
    assert skill_plugin.export_skill_plugin_env() is None
    assert skill_plugin.PLUGIN_DIR_ENV not in os.environ


def test_a_claude_we_cannot_find_still_gets_the_flag(home, sources, monkeypatch):
    """"Not on the server's PATH" is the NORMAL state on Windows — the templates
    search install locations this side deliberately does not — so an unfindable
    binary must not be read as an unsupporting one, or the skills get withheld
    from exactly the users least able to notice."""
    monkeypatch.setattr(skill_plugin, "_claude_bin", lambda: None)
    monkeypatch.setattr(skill_plugin, "_supports_plugin_dir",
                        lambda _b: pytest.fail("should not probe a missing bin"))
    assert skill_plugin.export_skill_plugin_env() == skill_plugin.plugin_dir()


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


def test_the_probe_fails_closed_and_is_cached(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        raise OSError("no such binary")

    monkeypatch.setattr(skill_plugin.subprocess, "run", fake_run)
    monkeypatch.setattr(skill_plugin, "_PLUGIN_DIR_SUPPORT", {})
    assert skill_plugin._supports_plugin_dir("/bin/nope") is False
    assert skill_plugin._supports_plugin_dir("/bin/nope") is False
    assert len(calls) == 1 and calls[0] == ["/bin/nope", "--help"]


@pytest.mark.parametrize("stdout,stderr,expected", [
    ("  --plugin-dir <path>\n", "", True),
    # stderr too: a shim that prints usage there is not an unsupporting CLI.
    ("", "  --plugin-dir <path>\n", True),
    ("  --verbose\n", "", False),
])
def test_the_probe_reads_the_help_text(stdout, stderr, expected, monkeypatch):
    monkeypatch.setattr(skill_plugin, "_PLUGIN_DIR_SUPPORT", {})
    monkeypatch.setattr(
        skill_plugin.subprocess, "run",
        lambda *a, **k: type("R", (), {"stdout": stdout, "stderr": stderr})())
    assert skill_plugin._supports_plugin_dir("/bin/claude") is expected


def test_the_real_cli_advertises_the_flag():
    """The one assertion here about the OUTSIDE world. Everything else mocks the
    probe, which means nothing above would notice if `--plugin-dir` were renamed
    or dropped — the feature would just quietly stop being used."""
    binary = skill_plugin._claude_bin()
    if binary is None:
        pytest.skip("no claude on PATH")
    skill_plugin._PLUGIN_DIR_SUPPORT.pop(binary, None)
    assert skill_plugin._supports_plugin_dir(binary) is True


@pytest.mark.parametrize("template", ["claude", "claude_split"])
def test_a_template_reads_the_root_through_appenv_only(template):
    """Both claude templates pass `--plugin-dir`, and neither may import
    `fused_render` (SPEC PY-15). They also must not REBUILD the path from
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
