"""Install and refresh the PUBLISHED ``fusedio/fused-render`` plugin in the
user's own Claude Code config (D492), so the skills reach sessions fused-render
did not launch — and so the user can SEE them.

This replaces the file copy this module used to be (``user_skills.py``, D185:
copy ``skills/<name>/`` into ``<CLAUDE_CONFIG_DIR>/skills/``). The job is
unchanged — cover the user's own ``claude``, in a terminal or in their app
folder, which ``--plugin-dir`` cannot reach because that flag only exists on the
processes we spawn ourselves. What changed is the mechanism, and it changed for a
reason the copy could never satisfy: a user who could not get a chat session to
know about ``fused.ai`` went looking on the Claude Config page — Marketplaces,
Plugins, Skills — and found nothing registered. A marketplace plugin is a thing
that page already lists, that ``claude plugin list`` already reports, that the
user can update, disable, or remove on their own terms. Loose marker-guarded
directories are none of those.

**We never enable, and never re-enable.** ``enabledPlugins`` carrying an explicit
``false`` for our id is the user having said no, and it ends this module's
involvement: no install, no marketplace add, no update, nothing. An absent key is
not a no — it is a machine that has never seen this plugin, which is what an
install is for.

**Nothing here is load-bearing, and it must not be.** The sessions fused-render
spawns get their skills from ``skill_plugin.py``'s locally assembled root, passed
as ``claude --plugin-dir`` from the installed wheel (D216). That path is offline,
synchronous, version-matched, and completely independent of everything below —
which is what makes it safe for this module to be a best-effort network call that
may fail, be rate-limited, be declined by the user, or never run at all. A
disabled plugin does not weaken a fused-render chat session by one skill.

Everything is best-effort and nothing raises: callers are server startup, where a
failure to reach GitHub may not fail the app.
"""
import json
import logging
import os
import shutil
import threading
import time

from fused_render.claude_config import lib
from fused_render.shell.storage import home_dir

logger = logging.getLogger(__name__)

# The published plugin, spelled the three ways the CLI needs it.
#
# `MARKETPLACE_NAME` is the `name` in the committed `.claude-plugin/
# marketplace.json`, and it is NOT derivable at runtime: a wheel ships the
# plugin manifest as a flat `skills/plugin.json` (nothing in a wheel may live
# under a dotted path) and no copy of the MARKETPLACE manifest at all. So it is
# a constant, pinned against the committed file by a test — the id the CLI and
# `installed_plugins.json` speak is `<plugin>@<marketplace>`, and getting either
# half wrong means silently never recognising our own plugin as installed.
MARKETPLACE_REF = "fusedio/fused-render"
MARKETPLACE_NAME = "fused-render"
PLUGIN_NAME = "fused-render"
PLUGIN_ID = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"

# How long before we touch the network again. Hours, not minutes: these are
# skills that change on release cadence, and the cost of asking is a git clone
# plus a cold start of the `claude` binary. Measured, not guessed: a full clone
# of this repo (marketplace `source: "./"`) is ~152MB for ~180KB of skills; the
# `--sparse` checkout below (see SPARSE_DIRS) brings that down to ~7MB, and the
# `claude` binary itself is a ~327MB executable that still has to start cold.
# Two intervals, because the two states are not equally urgent — an install
# that has not happened yet is a machine with no skills for its own sessions,
# while a refresh is a nicety.
_INSTALLED_REFRESH_S = 24 * 3600
_RETRY_S = 3600

# Beside the app's home dir, not inside the user's Claude config: this is OUR
# bookkeeping about a decision we made, and the config dir belongs to the CLI.
_STAMP_NAME = "user-plugin.json"

# The marker the DELETED file-copy sync (`user_skills.py`, D185) dropped into
# every dir it wrote, so a later run could tell "ours, safe to overwrite" apart
# from "user-authored, leave alone". That module is gone, but on any machine
# that ever ran it the marked dirs are still sitting under the skills dir —
# loadable, listed on the Skills page, and never refreshed again now that
# nothing writes them. This name must NEVER change: it is the only thing that
# still identifies those directories as ours, and there is no other record of
# which dirs D185 created.
_LEGACY_MARKER = ".managed-by-fused-render"

# `.claude-plugin/marketplace.json` declares this plugin's `source` as `"./"`
# — the whole repo — so an unrestricted `marketplace add` clones everything:
# frontend/, docs/, tests/, the lot. Measured: ~152MB for ~180KB of skills.
# `--sparse <dirs>` limits the checkout via git sparse-checkout. The dirs here
# are read off the committed manifests, not guessed: `.claude-plugin/
# plugin.json` declares no `commands`/`agents`/`hooks` override paths (and
# this repo has no `commands/`, `agents/` or `hooks/` dirs at its root to
# declare), so `skills/` is the only component dir the plugin needs — plus
# `.claude-plugin` itself, since that is where both manifests live and
# `marketplace add`/`plugin install` have to read them to work at all.
# Verified empirically against a throwaway CLAUDE_CONFIG_DIR: sparse checkout
# is ~7.0MB, still contains all 5 `fused-render-*` skill dirs, and `claude
# plugin list` reports the plugin installed and enabled exactly as a full
# clone does.
SPARSE_DIRS = (".claude-plugin", "skills")


def _config_dir() -> str:
    """Resolve the config dir the way the CHILD `claude` PROCESS will.

    `lib.CLAUDE_DIR` (and so `lib.SETTINGS_PATH` / `lib.INSTALLED_PLUGINS_PATH`)
    reads only `CLAUDE_DIR` (lib.py's own `CLAUDE_DIR` line) — it does NOT
    honour `CLAUDE_CONFIG_DIR`. But `lib.claude_cli` spawns `claude` with
    `{**os.environ}` unfiltered, and Claude Code's own CLI checks
    `CLAUDE_CONFIG_DIR` first, so when that var is set the `claude plugin
    install`/`update` we spawn below writes into a dir `lib`'s constants never
    point at. This module's entire job is to agree with the `claude` process
    it spawns — reading a different dir than that process writes to is
    exactly how `installed()` ends up permanently False (so `_install()`
    reruns every hour forever) and how `opted_out()` goes blind to a `false`
    the user put in the dir the CLI actually wrote, so `plugin install`
    re-enables a plugin the user turned off. So: read through THIS function,
    not through `lib`'s path constants.

    Same ordering, for the same reason, as `claude_health.config_dir()`
    (read its docstring too) — `CLAUDE_CONFIG_DIR` first because it is Claude
    Code's own var and wins, `CLAUDE_DIR` second only because `lib.py` has
    always keyed off it (so a dev/test that sets it still gets agreement),
    `~/.claude` last.
    """
    return (os.environ.get("CLAUDE_CONFIG_DIR")
            or os.environ.get("CLAUDE_DIR")
            or os.path.expanduser("~/.claude"))


def _now() -> int:
    """Wall clock, as a seam. Named so a test can move time without patching
    `time.time` itself — logging reads that too, and a stub that calls back into
    it recurses through the first log line."""
    return int(time.time())


def _stamp_path() -> str:
    return os.path.join(home_dir(), _STAMP_NAME)


# Defect 4: the rate limit must not fail open just because the STAMP FILE
# couldn't be written — a read-only or full disk is exactly the machine where
# a `claude` clone is least likely to succeed either, so that is precisely
# where we most need the gate to hold. `_fallback_stamp` is this process's own
# in-memory copy of the last stamp it produced (write attempt or not), used
# only when the disk copy is not at least as recent — so it can never make a
# genuinely fresh machine (no disk stamp, no prior attempt in this process)
# look like anything other than fresh, and it holds for the life of this
# process, which covers the restart-loop case the docstring above promises.
_fallback_stamp: dict = {}


def _read_stamp() -> dict:
    try:
        with open(_stamp_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        disk = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        disk = {}
    # Prefer disk whenever it is at least as fresh: the common, working case.
    # The fallback only wins when THIS process wrote something more recent
    # than what is (or is not) on disk — i.e. the write below failed silently.
    # A stamp with no usable timestamp sorts oldest, so a corrupt disk stamp
    # loses to a real in-process one and two absent ones tie (fresh machine).
    return disk if _attempted(disk) >= _attempted(_fallback_stamp) else dict(
        _fallback_stamp)


def _attempted(stamp: dict) -> int:
    """A stamp's attempt time, with anything unusable sorting oldest."""
    ts = stamp.get("attempted")
    return ts if isinstance(ts, int) else -1


def _write_stamp(installed: bool) -> None:
    """Recorded per ATTEMPT, not per success. A machine with no network must not
    retry on a loop, and the interval is the only thing standing between a
    restart loop and a `claude` spawn per restart.

    Defect 2: `ever_installed` is sticky (once True, stays True) — it is what
    lets a later run tell "fresh machine, never installed" apart from "the
    user uninstalled it", both of which look identical as `installed() ==
    False`. Only the caller (`sync_user_plugin`) can tell those apart, using
    this flag, because only it also has `installed()`'s CURRENT answer.
    """
    global _fallback_stamp
    stamp = {
        "attempted": _now(),
        "installed": installed,
        "ever_installed": bool(_read_stamp().get("ever_installed")) or installed,
    }
    # Set the in-process fallback unconditionally, BEFORE the disk write is
    # even attempted: if the write below fails, this is the only record left,
    # and it must reflect this attempt, not the previous one.
    _fallback_stamp = stamp
    try:
        os.makedirs(os.path.dirname(_stamp_path()), exist_ok=True)
        with open(_stamp_path(), "w", encoding="utf-8") as fh:
            json.dump(stamp, fh)
    except OSError as exc:
        logger.debug("could not write the user-plugin stamp: %s", exc)


def opted_out() -> bool:
    """Whether the user has explicitly turned this plugin OFF.

    An explicit `false` only. A MISSING key is not consent withheld — it is a
    machine that has never had this plugin — and reading it as a refusal would
    mean never installing on any machine, i.e. this module doing nothing at all,
    forever, on exactly the fresh installs it exists for.
    """
    # Read through `_config_dir()`, not `lib.read_settings()` / `lib.
    # SETTINGS_PATH`: those are keyed off `lib.CLAUDE_DIR`, which does not
    # honour `CLAUDE_CONFIG_DIR` (see `_config_dir()`'s docstring), so on a
    # machine with that var set they'd read a settings.json the `claude` we
    # spawn never touches — missing exactly the `false` this function exists
    # to find.
    settings = lib.read_json(os.path.join(_config_dir(), "settings.json"), {})
    enabled = settings.get("enabledPlugins")
    if not isinstance(enabled, dict):
        return False
    return enabled.get(PLUGIN_ID) is False


def installed() -> bool:
    """Whether the CLI's own record says our plugin is installed.

    Same `_config_dir()` reasoning as `opted_out()`: `lib.INSTALLED_PLUGINS_
    PATH` is keyed off `lib.CLAUDE_DIR` and is blind to `CLAUDE_CONFIG_DIR`, so
    reading it here could report False forever even after `claude plugin
    install` (spawned with `CLAUDE_CONFIG_DIR` in its environment) succeeded.
    """
    path = os.path.join(_config_dir(), "plugins", "installed_plugins.json")
    plugins = lib.read_json(path, {}).get("plugins")
    return isinstance(plugins, dict) and PLUGIN_ID in plugins


def _due(is_installed: bool) -> bool:
    stamp = _read_stamp()
    last = stamp.get("attempted")
    if not isinstance(last, int):
        return True
    interval = _INSTALLED_REFRESH_S if is_installed else _RETRY_S
    return _now() - last >= interval


def sync_user_plugin(force: bool = False) -> dict:
    """Install our plugin, or update it if it is already there.

    Returns a small record of what happened — for the log, and so a test can
    assert the DECISION rather than having to observe a `claude` invocation.
    `force` skips only the rate limit; it does not override an opt-out, because
    the opt-out is the user's and no caller of this outranks it.

    Never raises.
    """
    try:
        if opted_out():
            # Deliberately not stamped: this is not an attempt that needs
            # spacing out, it is a decision that costs nothing to re-read, and
            # stamping it would delay noticing that the user re-enabled us.
            return {"action": "skipped", "reason": "disabled by the user"}
        is_installed = installed()
        # Defect 2: `claude plugin uninstall` drops the `installed_plugins.
        # json` record WITHOUT leaving an explicit `false` in `enabledPlugins`
        # — so a removal looks identical to a fresh machine: `installed()` is
        # False and `opted_out()` is False (an absent key is deliberately not
        # a refusal). The stamp's sticky `ever_installed` is the only thing
        # that tells them apart: absent on a machine we've never touched is a
        # fresh install, which is what this module is for; absent on a
        # machine we HAVE installed on before is the user having removed it,
        # and reinstalling behind their back would be the same disrespect as
        # silently re-enabling a `false` — so stand down, permanently, same
        # as an opt-out (not stamped again: nothing changed to record).
        if not is_installed and _read_stamp().get("ever_installed"):
            return {"action": "skipped", "reason": "removed by the user"}
        if not force and not _due(is_installed):
            return {"action": "skipped", "reason": "checked recently"}
        # Stamped BEFORE the CLI runs (per-attempt, not per-success — see
        # `_write_stamp`'s docstring), with `is_installed` as it stood before
        # this attempt: on a FRESH install that is False, so it alone cannot
        # be what sets `ever_installed`. It is only ever flipped to True below,
        # once an install has actually SUCCEEDED — that is the one fact this
        # module must remember forever, because it is the only way a later
        # `installed() == False` can be told apart from a fresh machine.
        _write_stamp(is_installed)
        if is_installed:
            return _update()
        result = _install()
        if result.get("ok"):
            _write_stamp(True)
        return result
    except Exception as exc:  # noqa: BLE001 — a startup hook may not fail
        logger.warning("could not sync the %s plugin: %s", PLUGIN_ID, exc,
                       exc_info=True)
        return {"action": "failed", "reason": str(exc)}


def _skills_dir() -> str:
    """Resolved through `_config_dir()`, same reasoning as `opted_out()` and
    `installed()`: the old file-copy sync (D185) wrote into whatever dir the
    `claude` invocation of ITS day used, so cleanup has to look in the dir
    the CURRENT `claude` spawn agrees on too, or it cleans up nothing on any
    machine with `CLAUDE_CONFIG_DIR` set."""
    return os.path.join(_config_dir(), "skills")


def _remove_legacy_skill_dirs() -> list:
    """Delete every immediate child of the skills dir that carries
    `_LEGACY_MARKER`, and nothing else.

    The marker is the ONLY signal we have: D185 kept no separate manifest of
    what it wrote, so a dir without the marker is indistinguishable from one
    the user wrote by hand, and must never be touched, no matter how much its
    name looks like one of ours. Best-effort per directory — one `rmtree`
    failure (a file open elsewhere, a permissions quirk) must not stop the
    rest of the cleanup or bubble up to the caller, since this runs from a
    server startup thread.

    Returns the names removed, so a caller can log a count and a test can
    assert on exactly what was and was not touched.
    """
    removed = []
    try:
        entries = os.listdir(_skills_dir())
    except OSError:
        # No skills dir at all (nothing has ever synced here, or the user
        # deleted it) is the common, unremarkable case — not an error.
        return removed
    for name in entries:
        path = os.path.join(_skills_dir(), name)
        marker = os.path.join(path, _LEGACY_MARKER)
        if not os.path.isfile(marker):
            # No marker: either user-authored, or not a dir at all. Either
            # way, ours to leave alone.
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:
            logger.debug("could not remove legacy skill dir %r: %s", name, exc)
            continue
        removed.append(name)
        logger.info("removed legacy skill dir %r left behind by the old "
                    "file-copy sync", name)
    return removed


def _install() -> dict:
    """Add the marketplace, then install from it.

    The marketplace add is NOT checked: it fails when the marketplace is already
    known, which is the common case on a machine where the user (or a previous
    run) added it, and treating that as fatal would make the install unreachable
    exactly where it is most likely to succeed. The install's own result is the
    one that means something.

    `-y` because this runs headless and the CLI requires it when stdout is not a
    TTY; a prompt here would just wait out the timeout. The timeout is generous
    because this clones a repo — `claude_cli`'s 25s default is a local-command
    budget and would report a working install as a failure.
    """
    lib.claude_cli("plugin", "marketplace", "add", MARKETPLACE_REF,
                   "--sparse", *SPARSE_DIRS, "--scope", "user", timeout=120)
    res = lib.claude_cli("plugin", "install", PLUGIN_ID,
                         "--scope", "user", "-y", timeout=120)
    if not res["ok"]:
        logger.info("could not install %s: %s", PLUGIN_ID,
                    res["stderr"] or "no error reported")
        return {"action": "install", "ok": False,
                "error": res["stderr"] or "install failed"}
    logger.info("installed the %s plugin for the user's own sessions", PLUGIN_ID)
    result = {"action": "install", "ok": True}
    # Only retire the old delivery once the new one is actually in place: a
    # failed install must never leave the user with no skills at all, so this
    # only runs after `res["ok"]` above is True.
    removed = _remove_legacy_skill_dirs()
    if removed:
        result["removed_legacy"] = removed
    return result


def _update() -> dict:
    """Refresh an installed plugin. Also refreshes the MARKETPLACE first: the
    plugin is installed from a clone of it, so a stale clone has nothing newer
    to give and `plugin update` would truthfully report no change."""
    lib.claude_cli("plugin", "marketplace", "update", MARKETPLACE_NAME,
                   timeout=120)
    res = lib.claude_cli("plugin", "update", PLUGIN_ID,
                         "--scope", "user", "-y", timeout=120)
    if not res["ok"]:
        logger.info("could not update %s: %s", PLUGIN_ID,
                    res["stderr"] or "no error reported")
        return {"action": "update", "ok": False,
                "error": res["stderr"] or "update failed"}
    result = {"action": "update", "ok": True}
    # Same rationale as `_install`: only clean up the legacy dirs once we know
    # the plugin's own content is actually current.
    removed = _remove_legacy_skill_dirs()
    if removed:
        result["removed_legacy"] = removed
    return result


_started = False
_start_lock = threading.Lock()


def start() -> None:
    """Run the sync on a daemon thread, once per process.

    A THREAD, unlike the file copy this replaced: the work is a `claude` spawn
    that clones over the network, and the caller is a startup event that runs
    before the server serves. D228 is the precedent that matters — a single
    `claude --help` on the pre-bind path, against a cold binary facing a Windows
    Defender first-touch scan, outran the desktop supervisor's 20s readiness
    budget and killed the server three times. Nothing here is load-bearing
    enough to be worth one second of startup, let alone a clone.

    Once per process via a flag, NOT via a live-thread check the way
    `ai.supervisor`'s reaper does it: that thread loops forever, so liveness and
    "already started" mean the same thing there. This one runs a single pass and
    exits, so a liveness check would let every later `create_app` start another
    — the suite builds many in one process, and each would re-read the stamp and
    spawn `claude` if the interval happened to be up. Refreshing across a longer
    span is the STAMP's job, not this hook's.
    """
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    def run() -> None:
        logger.debug("user plugin sync: %s", sync_user_plugin())

    threading.Thread(target=run, daemon=True, name="fused-user-plugin").start()
