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
# skills that change on release cadence, and the cost of asking is a clone plus
# a cold start of a ~279MB Node binary. Two intervals, because the two states
# are not equally urgent — an install that has not happened yet is a machine
# with no skills for its own sessions, while a refresh is a nicety.
_INSTALLED_REFRESH_S = 24 * 3600
_RETRY_S = 3600

# Beside the app's home dir, not inside the user's Claude config: this is OUR
# bookkeeping about a decision we made, and the config dir belongs to the CLI.
_STAMP_NAME = "user-plugin.json"


def _now() -> int:
    """Wall clock, as a seam. Named so a test can move time without patching
    `time.time` itself — logging reads that too, and a stub that calls back into
    it recurses through the first log line."""
    return int(time.time())


def _stamp_path() -> str:
    return os.path.join(home_dir(), _STAMP_NAME)


def _read_stamp() -> dict:
    try:
        with open(_stamp_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_stamp(installed: bool) -> None:
    """Recorded per ATTEMPT, not per success. A machine with no network must not
    retry on a loop, and the interval is the only thing standing between a
    restart loop and a `claude` spawn per restart."""
    try:
        os.makedirs(os.path.dirname(_stamp_path()), exist_ok=True)
        with open(_stamp_path(), "w", encoding="utf-8") as fh:
            json.dump({"attempted": _now(), "installed": installed}, fh)
    except OSError as exc:
        logger.debug("could not write the user-plugin stamp: %s", exc)


def opted_out() -> bool:
    """Whether the user has explicitly turned this plugin OFF.

    An explicit `false` only. A MISSING key is not consent withheld — it is a
    machine that has never had this plugin — and reading it as a refusal would
    mean never installing on any machine, i.e. this module doing nothing at all,
    forever, on exactly the fresh installs it exists for.
    """
    settings = lib.read_settings()
    enabled = settings.get("enabledPlugins")
    if not isinstance(enabled, dict):
        return False
    return enabled.get(PLUGIN_ID) is False


def installed() -> bool:
    """Whether the CLI's own record says our plugin is installed."""
    plugins = lib.read_json(lib.INSTALLED_PLUGINS_PATH, {}).get("plugins")
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
        if not force and not _due(is_installed):
            return {"action": "skipped", "reason": "checked recently"}
        _write_stamp(is_installed)
        if is_installed:
            return _update()
        return _install()
    except Exception as exc:  # noqa: BLE001 — a startup hook may not fail
        logger.warning("could not sync the %s plugin: %s", PLUGIN_ID, exc,
                       exc_info=True)
        return {"action": "failed", "reason": str(exc)}


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
                   "--scope", "user", timeout=120)
    res = lib.claude_cli("plugin", "install", PLUGIN_ID,
                         "--scope", "user", "-y", timeout=120)
    if not res["ok"]:
        logger.info("could not install %s: %s", PLUGIN_ID,
                    res["stderr"] or "no error reported")
        return {"action": "install", "ok": False,
                "error": res["stderr"] or "install failed"}
    logger.info("installed the %s plugin for the user's own sessions", PLUGIN_ID)
    return {"action": "install", "ok": True}


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
    return {"action": "update", "ok": True}


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
