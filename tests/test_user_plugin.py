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
    """A scratch Claude config, repointed the way test_claude_config_api does:
    lib resolves its paths from the env once at import, so setting the env var
    in a test is too late."""
    root = tmp_path / "claude-home"
    root.mkdir()
    monkeypatch.setattr(lib, "CLAUDE_DIR", str(root))
    monkeypatch.setattr(lib, "SETTINGS_PATH", str(root / "settings.json"))
    monkeypatch.setattr(lib, "INSTALLED_PLUGINS_PATH",
                        str(root / "plugins" / "installed_plugins.json"))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    return root


class _Cli(list):
    """The recorded `claude` argvs, plus `fail` — the subcommands a test wants
    to come back non-zero. A list subclass so the assertions read as list
    comparisons, which is what they are."""

    fail: set


@pytest.fixture()
def cli(monkeypatch):
    """Record every `claude` argv instead of running one, and let a test say
    which invocation fails."""
    calls = _Cli()
    calls.fail = set()

    def fake(*args, timeout=25):
        calls.append(args)
        ok = not (calls.fail & set(args))
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


# -- what actually reaches the CLI -------------------------------------------

def test_the_install_adds_the_marketplace_then_installs_headlessly(claude_dir, cli):
    """-y on both, because the CLI requires it when stdout is not a TTY and this
    always runs headless; a prompt would just wait out the timeout."""
    res = user_plugin.sync_user_plugin()
    assert res == {"action": "install", "ok": True}
    add, install = cli
    assert add == ("plugin", "marketplace", "add", user_plugin.MARKETPLACE_REF,
                   "--scope", "user")
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
    monkeypatch.setattr(lib, "read_settings",
                        lambda: (_ for _ in ()).throw(OSError("nope")))
    assert user_plugin.sync_user_plugin()["action"] == "failed"


# -- the rate limit, which is what stands between a restart loop and a clone --

def test_a_recent_attempt_is_not_repeated(claude_dir, cli):
    assert user_plugin.sync_user_plugin()["action"] == "install"
    cli.clear()
    assert user_plugin.sync_user_plugin() == {"action": "skipped",
                                             "reason": "checked recently"}
    assert cli == []


def test_force_skips_the_rate_limit(claude_dir, cli):
    user_plugin.sync_user_plugin()
    cli.clear()
    assert user_plugin.sync_user_plugin(force=True)["action"] == "install"
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
