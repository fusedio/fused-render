"""The published-plugin sync (fused_render/user_plugin.py, D492): the
`fusedio/fused-render` plugin installed or refreshed in the user's OWN Claude
config, for sessions fused-render did not launch.

Every test here is about a DECISION, not about a `claude` invocation, so the CLI
is faked throughout and the assertions are on what was decided and what argv it
produced. The one rule with teeth: an explicit `false` in `enabledPlugins` ends
this module's involvement completely.
"""
import json
import os
import threading
import time

import pytest

from fused_render import user_plugin
from fused_render.claude_config import lib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def claude_dir(tmp_path, monkeypatch):
    """A scratch Claude config, addressed via `CLAUDE_CONFIG_DIR`, NOT via
    `lib.CLAUDE_DIR`/`lib.SETTINGS_PATH`/`lib.INSTALLED_PLUGINS_PATH`: this
    module's whole point (defect 1) is that it must resolve its config dir the
    same way the `claude` CHILD PROCESS does — `CLAUDE_CONFIG_DIR` first — not
    the way `lib` does (`CLAUDE_DIR` only, never `CLAUDE_CONFIG_DIR`). Setting
    the env var is what actually exercises `_config_dir()`; monkeypatching
    `lib`'s attributes, as this fixture used to, would silently test the OLD,
    wrong resolution and never notice it diverged from the CLI's own.
    """
    root = tmp_path / "claude-home"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    # The rate-limit's in-process fallback (defect 4) is a module global and
    # must not leak between tests sharing this pytest process.
    monkeypatch.setattr(user_plugin, "_fallback_stamp", {})
    return root


class _Cli(list):
    """The recorded `claude` argvs, plus `fail` — the subcommands a test wants
    to come back non-zero. A list subclass so the assertions read as list
    comparisons, which is what they are."""

    fail: set


@pytest.fixture()
def cli(monkeypatch):
    """Record every `claude` argv instead of running one, and let a test say
    which invocation fails.

    A successful `plugin install`/`plugin update` also writes a real
    `installed_plugins.json` into `CLAUDE_CONFIG_DIR` — mirroring what the
    real CLI actually does. Without this, `installed()` (which reads that
    file for real, per the defect-1 fix) would stay False forever even after
    a "successful" fake install, which is not how the real CLI behaves and
    would make the defect-2 removal check (`ever_installed` but
    `installed() == False`) misfire on every second sync in this test suite,
    not just on an actual uninstall.
    """
    calls = _Cli()
    calls.fail = set()

    def fake(*args, timeout=25):
        calls.append(args)
        ok = not (calls.fail & set(args))
        if ok and args[:2] in (("plugin", "install"), ("plugin", "update")):
            config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
            path = os.path.join(config_dir, "plugins", "installed_plugins.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"plugins": {user_plugin.PLUGIN_ID: [
                    {"version": "1.0.0"}]}}, fh)
        return {"ok": ok, "stdout": "", "stderr": "" if ok else "boom"}

    monkeypatch.setattr(lib, "claude_cli", fake)
    return calls


def _settings(claude_dir, enabled):
    (claude_dir / "settings.json").write_text(
        json.dumps({"enabledPlugins": enabled}), encoding="utf-8")


def _installed(claude_dir):
    d = claude_dir / "plugins"
    d.mkdir(exist_ok=True)
    (d / "installed_plugins.json").write_text(
        json.dumps({"plugins": {user_plugin.PLUGIN_ID: [{"version": "1.0.0"}]}}),
        encoding="utf-8")


# -- the opt-out, which is the whole reason this module reads settings at all --

def test_an_explicitly_disabled_plugin_is_left_completely_alone(claude_dir, cli):
    """`false` is the user having said no, and it ends everything: no install, no
    marketplace add, no update. Not even a `claude` spawn — the point is that we
    stop, not that we stop after asking."""
    _settings(claude_dir, {user_plugin.PLUGIN_ID: False})
    res = user_plugin.sync_user_plugin()
    assert res == {"action": "skipped", "reason": "disabled by the user"}
    assert cli == []


def test_force_does_not_override_the_users_no(claude_dir, cli):
    """`force` exists to skip the rate limit. The opt-out is not a rate limit,
    and no caller of this outranks the user."""
    _settings(claude_dir, {user_plugin.PLUGIN_ID: False})
    assert user_plugin.sync_user_plugin(force=True)["action"] == "skipped"
    assert cli == []


def test_a_missing_key_is_not_a_refusal(claude_dir, cli):
    """The distinction the whole module turns on: absent means "this machine has
    never had it", which is what an install is FOR. Reading it as a refusal
    would mean never installing anywhere, i.e. doing nothing forever."""
    _settings(claude_dir, {"someone-else@market": False})
    assert user_plugin.opted_out() is False
    assert user_plugin.sync_user_plugin()["action"] == "install"


def test_a_disabled_OTHER_plugin_does_not_stop_us(claude_dir, cli):
    _settings(claude_dir, {"unrelated@market": False})
    assert user_plugin.sync_user_plugin()["action"] == "install"
    assert cli


def test_an_enabled_plugin_is_updated_not_reinstalled(claude_dir, cli):
    _settings(claude_dir, {user_plugin.PLUGIN_ID: True})
    _installed(claude_dir)
    assert user_plugin.sync_user_plugin() == {"action": "update", "ok": True}
    assert [a[:2] for a in cli] == [("plugin", "marketplace"), ("plugin", "update")]


def test_reads_CLAUDE_CONFIG_DIR_not_libs_own_CLAUDE_DIR(claude_dir, cli):
    """The defect this pins: `lib.CLAUDE_DIR` never reads `CLAUDE_CONFIG_DIR`
    (only `CLAUDE_DIR`, then `~/.claude`), but the `claude` CHILD PROCESS this
    module spawns honours `CLAUDE_CONFIG_DIR` first. `claude_dir` points
    `CLAUDE_CONFIG_DIR` — and only that — at a dir whose settings.json
    disables our plugin. Against the pre-fix code (which read `lib.
    SETTINGS_PATH`, keyed off `lib.CLAUDE_DIR`, i.e. this process's real
    `CLAUDE_DIR`/`~/.claude`) that `false` is invisible and a `claude` spawn
    happens anyway. Fixed code must see it and make NO CLI call at all."""
    _settings(claude_dir, {user_plugin.PLUGIN_ID: False})
    assert user_plugin.opted_out() is True
    res = user_plugin.sync_user_plugin()
    assert res == {"action": "skipped", "reason": "disabled by the user"}
    assert cli == []


# -- what actually reaches the CLI -------------------------------------------

def test_the_install_adds_the_marketplace_then_installs_headlessly(claude_dir, cli):
    """-y on both, because the CLI requires it when stdout is not a TTY and this
    always runs headless; a prompt would just wait out the timeout."""
    res = user_plugin.sync_user_plugin()
    assert res == {"action": "install", "ok": True}
    add, install = cli
    assert add == ("plugin", "marketplace", "add", user_plugin.MARKETPLACE_REF,
                   "--sparse", *user_plugin.SPARSE_DIRS, "--scope", "user")
    assert install == ("plugin", "install", user_plugin.PLUGIN_ID,
                       "--scope", "user", "-y")


def test_an_already_known_marketplace_does_not_block_the_install(claude_dir, cli):
    """`marketplace add` fails when the marketplace is already there — the
    common case on a machine where the user added it themselves. Treating that
    as fatal would make the install unreachable exactly where it would work."""
    cli.fail.add("add")
    assert user_plugin.sync_user_plugin() == {"action": "install", "ok": True}


def test_a_failed_install_is_reported_not_raised(claude_dir, cli):
    """The caller is a startup thread; a machine with no network must get a
    working app."""
    cli.fail.add("install")
    res = user_plugin.sync_user_plugin()
    assert res["action"] == "install" and res["ok"] is False
    assert res["error"] == "boom"


def test_nothing_raises_when_the_config_cannot_be_read(claude_dir, cli, monkeypatch):
    # `opted_out()` now reads through `lib.read_json` (against `_config_dir()`,
    # not `lib.SETTINGS_PATH`) rather than `lib.read_settings`, so that is the
    # seam to break here.
    monkeypatch.setattr(lib, "read_json",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    assert user_plugin.sync_user_plugin()["action"] == "failed"


# -- the rate limit, which is what stands between a restart loop and a clone --

def test_a_recent_attempt_is_not_repeated(claude_dir, cli):
    assert user_plugin.sync_user_plugin()["action"] == "install"
    cli.clear()
    assert user_plugin.sync_user_plugin() == {"action": "skipped",
                                             "reason": "checked recently"}
    assert cli == []


def test_force_skips_the_rate_limit(claude_dir, cli):
    """`force` skips the rate limit only — whichever action the CLI's own
    state calls for still runs. A successful first install means the CLI now
    reports the plugin installed, so a forced second sync is an UPDATE, not a
    second install; either way the point is it isn't skipped."""
    user_plugin.sync_user_plugin()
    cli.clear()
    assert user_plugin.sync_user_plugin(force=True)["action"] == "update"
    assert cli


def test_the_stamp_is_written_per_attempt_not_per_success(claude_dir, cli):
    """A machine with no network must not retry on a loop — so the stamp goes
    down before the CLI is called, not after it succeeds."""
    cli.fail.add("install")
    user_plugin.sync_user_plugin()
    assert os.path.isfile(user_plugin._stamp_path())
    cli.clear()
    assert user_plugin.sync_user_plugin()["reason"] == "checked recently"


def test_an_installed_plugin_gets_the_longer_interval(claude_dir, cli, monkeypatch):
    """A pending install is a machine with no skills for its own sessions; a
    refresh is a nicety. The two intervals say which is which."""
    assert user_plugin._RETRY_S < user_plugin._INSTALLED_REFRESH_S
    _installed(claude_dir)
    user_plugin.sync_user_plugin()
    # Move the CLOCK the module reads, not `time.time` itself — logging calls
    # that too, and patching it with something that calls it back recurses.
    later = int(time.time()) + user_plugin._RETRY_S + 60
    monkeypatch.setattr(user_plugin, "_now", lambda: later)
    # Just past the retry window, still inside the refresh window.
    assert user_plugin.sync_user_plugin()["reason"] == "checked recently"


def test_a_corrupt_stamp_is_treated_as_no_stamp(claude_dir, cli):
    os.makedirs(os.path.dirname(user_plugin._stamp_path()), exist_ok=True)
    with open(user_plugin._stamp_path(), "w") as fh:
        fh.write("not json")
    assert user_plugin.sync_user_plugin()["action"] == "install"


def test_a_stamp_write_failure_still_rate_limits(claude_dir, cli, monkeypatch,
                                                 tmp_path):
    """Defect 4: `_write_stamp` swallowing `OSError` must not mean the rate
    limit fails open. A read-only/full disk is exactly the machine where the
    write below fails AND a `claude` clone is least likely to work either —
    which is exactly where the gate matters most. Force the write to fail by
    making its directory a plain FILE (so `os.makedirs(..., exist_ok=True)`
    raises `FileExistsError`), and check the in-process fallback still holds
    the gate for the rest of this process."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(user_plugin, "_stamp_path",
                        lambda: str(blocker / "user-plugin.json"))
    assert user_plugin.sync_user_plugin()["action"] == "install"
    assert not blocker.is_dir()  # the write really failed, not silently no-op
    cli.clear()
    assert user_plugin.sync_user_plugin() == {"action": "skipped",
                                             "reason": "checked recently"}
    assert cli == []


# -- retiring the legacy file-copy dirs (D185) --------------------------------
#
# The deleted `user_skills.py` copied `skills/<name>/` into the user's own
# skills dir and dropped `_LEGACY_MARKER` inside each copy. Nothing removes
# those copies now that the module is gone, so they sit there forever,
# shadowing the plugin's own content. These tests pin the cleanup: marked
# dirs go, everything else survives, and it only runs once the new delivery
# (the plugin) is actually confirmed in place.

def _legacy_dir(claude_dir, name, marked=True):
    """A skills-dir child shaped like something D185 could have written (or,
    with `marked=False`, like a user's own same-named skill)."""
    d = claude_dir / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("hi", encoding="utf-8")
    if marked:
        (d / user_plugin._LEGACY_MARKER).write_text("", encoding="utf-8")
    return d


def test_a_marker_bearing_legacy_dir_is_removed_after_a_successful_install(
        claude_dir, cli):
    """The whole point of the cleanup: a dir D185 wrote and marked is ours to
    delete, and it goes once we know the plugin took its place."""
    legacy = _legacy_dir(claude_dir, "fused-render-authoring")
    res = user_plugin.sync_user_plugin()
    assert res["action"] == "install" and res["ok"] is True
    assert res["removed_legacy"] == ["fused-render-authoring"]
    assert not legacy.exists()


def test_an_unmarked_dir_with_a_matching_name_is_never_touched(claude_dir, cli):
    """No marker means no proof it's ours — even when the name matches one of
    our own skills exactly, it might be the user's own work, and deleting it
    on a guess is exactly what D185's marker existed to prevent."""
    theirs = _legacy_dir(claude_dir, "fused-render-authoring", marked=False)
    res = user_plugin.sync_user_plugin()
    assert res["action"] == "install" and res["ok"] is True
    assert "removed_legacy" not in res
    assert theirs.exists()
    assert (theirs / "SKILL.md").exists()


def test_nothing_is_removed_when_the_install_fails(claude_dir, cli):
    """A failed install means the new delivery is NOT in place. Retiring the
    old one anyway would leave the user with no skills at all on exactly the
    offline/rate-limited machine where they need them most."""
    legacy = _legacy_dir(claude_dir, "fused-render-authoring")
    cli.fail.add("install")
    res = user_plugin.sync_user_plugin()
    assert res["ok"] is False
    assert "removed_legacy" not in res
    assert legacy.exists()


def test_nothing_is_removed_when_the_user_has_opted_out(claude_dir, cli):
    """An opt-out means we are no longer serving this user at all. Those
    leftover copies are then the only fused skills they have, and silently
    deleting files for someone we don't serve is worse than leaving them."""
    _settings(claude_dir, {user_plugin.PLUGIN_ID: False})
    legacy = _legacy_dir(claude_dir, "fused-render-authoring")
    res = user_plugin.sync_user_plugin()
    assert res["action"] == "skipped"
    assert legacy.exists()
    assert cli == []


def test_a_missing_skills_dir_is_not_an_error(claude_dir, cli):
    """The common case on a fresh machine — nothing has ever synced skills
    here — must be indistinguishable from "nothing to clean up", not a
    failure."""
    assert not os.path.isdir(os.path.join(str(claude_dir), "skills"))
    res = user_plugin.sync_user_plugin()
    assert res["action"] == "install" and res["ok"] is True
    assert "removed_legacy" not in res


# -- an uninstall is a decision too, not a fresh machine (defect 2) ----------

def test_an_uninstall_is_not_silently_undone(claude_dir, cli):
    """`claude plugin uninstall` drops the `installed_plugins.json` record
    without leaving an explicit `false` in `enabledPlugins`, so a removal
    looks identical to a fresh machine to both `installed()` (False) and
    `opted_out()` (False — absent is not a refusal). The sticky
    `ever_installed` stamp is the only thing that tells them apart, and it
    must win: once we've installed successfully, a later `installed() ==
    False` is the user having removed it, and must NOT be reinstalled — not
    even with `force`, since standing down here is the same kind of respect
    for the user's choice as the opt-out, not a rate limit `force` may skip.
    """
    assert user_plugin.sync_user_plugin()["action"] == "install"
    cli.clear()
    # Simulate the uninstall: the CLI's own record disappears; nothing else
    # about the machine changes. (The CLI is faked, so nothing wrote this
    # file for real — create the dir the way the real CLI would have.)
    (claude_dir / "plugins").mkdir(exist_ok=True)
    (claude_dir / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {}}), encoding="utf-8")
    res = user_plugin.sync_user_plugin(force=True)
    assert res == {"action": "skipped", "reason": "removed by the user"}
    assert cli == []


def test_a_fresh_machine_with_no_stamp_is_still_a_fresh_install(claude_dir, cli):
    """The other half of the same distinction: `installed() == False` with NO
    prior stamp at all (never installed here before) must still install —
    `ever_installed` being absent, not True, is what tells this apart from
    the removal case above."""
    assert not os.path.isfile(user_plugin._stamp_path())
    assert user_plugin.sync_user_plugin()["action"] == "install"


# -- the ids, which nothing can derive at runtime -----------------------------

def test_the_ids_match_the_committed_manifests():
    """`<plugin>@<marketplace>` is the id the CLI and installed_plugins.json
    speak, and neither half is derivable on a wheel: it ships the PLUGIN
    manifest as a flat skills/plugin.json and no marketplace manifest at all.
    So both are constants here, and this is what stops them drifting from the
    committed files that actually define them."""
    with open(os.path.join(REPO_ROOT, ".claude-plugin", "marketplace.json"),
              encoding="utf-8") as fh:
        market = json.load(fh)
    with open(os.path.join(REPO_ROOT, ".claude-plugin", "plugin.json"),
              encoding="utf-8") as fh:
        plugin = json.load(fh)

    assert user_plugin.MARKETPLACE_NAME == market["name"]
    assert user_plugin.PLUGIN_NAME == plugin["name"]
    # The marketplace publishes the plugin we claim to install.
    assert user_plugin.PLUGIN_NAME in {p["name"] for p in market["plugins"]}
    assert user_plugin.PLUGIN_ID == (
        f"{user_plugin.PLUGIN_NAME}@{user_plugin.MARKETPLACE_NAME}")
    # …and the ref is this repo, which is what `marketplace add` clones.
    assert user_plugin.MARKETPLACE_REF == "fusedio/fused-render"


def test_sparse_dirs_cover_everything_the_manifests_declare_and_nothing_else():
    """Defect 3: `--sparse` must list every dir the plugin actually needs, read
    off the committed manifests rather than guessed. `.claude-plugin/
    plugin.json` declares no `commands`/`agents`/`hooks` override paths, and
    this repo has no such directories at its root to declare — `skills/` is
    the only component dir, plus `.claude-plugin` itself (where both manifests
    live). If a future PR adds a top-level `commands/`/`agents/`/`hooks/`
    without updating `SPARSE_DIRS`, that content would silently never reach a
    sparse checkout — this is the tripwire for that."""
    assert set(user_plugin.SPARSE_DIRS) == {".claude-plugin", "skills"}
    for d in user_plugin.SPARSE_DIRS:
        assert os.path.isdir(os.path.join(REPO_ROOT, d))
    for undeclared in ("commands", "agents", "hooks"):
        assert not os.path.isdir(os.path.join(REPO_ROOT, undeclared))


def test_start_runs_once_per_process(claude_dir, cli, monkeypatch):
    """The startup hook runs once per `create_app` and the suite builds many in
    one process. Guarded by a FLAG, not by a live-thread check: this thread runs
    one pass and exits, so liveness would say "not started" a millisecond later
    and every later app would spawn another."""
    monkeypatch.setattr(user_plugin, "_started", False)
    done = threading.Event()
    monkeypatch.setattr(user_plugin, "sync_user_plugin",
                        lambda: (done.set(), {"action": "install"})[1])
    user_plugin.start()
    assert done.wait(timeout=5)
    done.clear()
    user_plugin.start()
    assert not done.wait(timeout=0.2)
