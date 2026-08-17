"""The OS-side half of scheduled messages: make sure the app is RUNNING when a
message comes due.

`schedule.py` owns the schedule, the send, and every decision in between — for
the reasons in its docstring, all of which come down to a scheduled turn needing
the app's environment, credentials and (on macOS) TCC identity. The cost of that
choice is that nothing fires while the app is closed. This module buys back as
much of that as an OS can be asked for honestly: it does not send anything, it
does not know what a message is, it just asks the platform to *launch the app*
at the times something is due. The app then does what it always does on
startup — its first tick catches up whatever came due.

**macOS: a LaunchAgent.** `launchd` is the right mechanism and `cron` is not.
A per-user LaunchAgent runs inside the user's GUI (Aqua) session, so the app it
launches has the login Keychain and can prompt for consent like any other app
the user opened; `launchd` also runs a missed `StartCalendarInterval` when the
machine next wakes, which `cron` simply does not do. The agent's program is
`open -g -a <bundle>` — the same thing double-clicking the app does, only
without stealing focus — so nothing here needs to know how the app starts.

**Windows and Linux: nothing new, deliberately.** Both already have a
start-at-login toggle that the supervisor owns (`_win32/startup.py`'s Run key,
`_linux/startup.py`'s freedesktop autostart entry), and both platforms' packaged
app is a supervisor-managed process. Writing a second, schedule-specific timer
(a Task Scheduler task, a systemd user timer) would be a third mechanism that
can disagree with those two about whether the app should be running. What the
user gets on those platforms is therefore weaker and worth saying out loud: a
message fires if the app is running or gets started (including at login), and
waits if it is not.

Everything here is BEST-EFFORT and the caller treats it that way. A failure to
write a plist means messages fire a bit less reliably; it never means a message
is lost, and it must never fail the store write that triggered it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from xml.sax.saxutils import escape

LABEL = "io.fused.render.schedule-wake"

# How many distinct due times ride into one plist. launchd takes an array of
# StartCalendarInterval dicts happily, but the file is a wake-up list, not the
# schedule: the app's own store is the schedule, and any launch at all gets the
# whole overdue set fired by the first tick. So the soonest few are all that
# matter, and a user with 500 scheduled messages does not get a 500-entry plist.
MAX_INTERVALS = 20


def _is_darwin() -> bool:
    return sys.platform == "darwin"


def agent_plist_path() -> str:
    return os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def app_bundle_path() -> str | None:
    """The running app bundle (`…/FusedRender.app`), or None when there isn't
    one — a dev-from-source run, a plain wheel, a test.

    Nothing to relaunch is a perfectly ordinary state, not an error: `dev.sh`
    and `pytest` both live there. Returning None makes `sync` remove any plist
    it previously wrote rather than leave one pointing at a bundle this
    interpreter cannot vouch for."""
    path = os.path.abspath(sys.executable)
    while True:
        parent = os.path.dirname(path)
        if parent == path:  # hit the filesystem root
            return None
        if path.endswith(".app") and os.path.isdir(path):
            return path
        path = parent


def _calendar_intervals(due_utc: list[datetime]) -> list[dict]:
    """The soonest `MAX_INTERVALS` due times as launchd calendar dicts.

    **In LOCAL time.** `StartCalendarInterval` is evaluated against the user's
    own clock, and the schedule stores UTC — handing launchd a UTC hour would
    fire the app at the wrong time of day for every user not on UTC.

    Minute/Hour/Day/Month and no Year: launchd has no Year key, so an interval
    recurs annually. That is harmless here (a spurious wake once a year, at a
    time when the store holds nothing due, opens the app and fires nothing) and
    the alternative — omitting Day/Month to get a daily wake — is worse."""
    seen: list[dict] = []
    for when in sorted(due_utc)[:MAX_INTERVALS]:
        local = when.astimezone()
        interval = {"Minute": local.minute, "Hour": local.hour,
                    "Day": local.day, "Month": local.month}
        if interval not in seen:
            seen.append(interval)
    return seen


def plist_xml(app_path: str, due_utc: list[datetime]) -> str:
    """The LaunchAgent plist for these due times. Pure — the whole file as a
    string, so what gets installed is exactly what the tests read.

    `open -g -a` launches the app WITHOUT bringing it to the front: a wake at
    3am must not steal focus from whatever the user left on screen. No
    RunAtLoad (that would launch the app every login, which is the
    start-at-login toggle's decision to own, not this file's) and no KeepAlive
    (this launches an app, it does not supervise one)."""
    rows = []
    for interval in _calendar_intervals(due_utc):
        pairs = "".join(
            f"\n\t\t\t<key>{k}</key>\n\t\t\t<integer>{v}</integer>"
            for k, v in interval.items())
        rows.append(f"\t\t<dict>{pairs}\n\t\t</dict>")
    intervals = "\n".join(rows)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"\t<key>Label</key>\n\t<string>{escape(LABEL)}</string>\n"
        "\t<key>ProgramArguments</key>\n"
        "\t<array>\n"
        "\t\t<string>/usr/bin/open</string>\n"
        "\t\t<string>-g</string>\n"
        "\t\t<string>-a</string>\n"
        f"\t\t<string>{escape(app_path)}</string>\n"
        "\t</array>\n"
        "\t<key>StartCalendarInterval</key>\n"
        f"\t<array>\n{intervals}\n\t</array>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _launchctl(*args: str) -> None:
    """Run one launchctl subcommand, ignoring its exit status.

    Every call here is "make the world look like the file": booting out an agent
    that was never loaded fails, and so does bootstrapping one that already is.
    Both are the state we want, and neither is worth distinguishing from the
    other — the plist on disk is the durable half, and launchd re-reads it at
    the next login regardless."""
    try:
        subprocess.run(["/bin/launchctl", *args], capture_output=True,
                       timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def remove() -> bool:
    """Unload and delete the wake agent. True if a plist was actually removed."""
    if not _is_darwin():
        return False
    path = agent_plist_path()
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def sync(due: list) -> bool:
    """Point the wake agent at these pending due times. True if a plist is now
    installed for them.

    `due` is whatever the store holds — ISO strings, or datetimes — since the
    caller passes entries straight through. Unparseable values are dropped
    rather than raised on: this is a best-effort wake-up hint, and one malformed
    row must not cost the others their wake.

    An empty list (or a run with no app bundle to launch) removes the agent:
    a wake with nothing to fire is a machine woken for no reason."""
    if not _is_darwin():
        return False
    from fused_render.schedule import parse_due

    parsed: list[datetime] = []
    for value in due:
        if isinstance(value, datetime):
            parsed.append(value)
            continue
        try:
            parsed.append(parse_due(value))
        except ValueError:
            continue
    app = app_bundle_path()
    if not parsed or app is None:
        remove()
        return False

    path = agent_plist_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(plist_xml(app, parsed))
    # Reload so the change takes effect now rather than at the next login —
    # bootout first, because bootstrap on an already-loaded label is a no-op
    # that would leave the OLD intervals live.
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    _launchctl("bootstrap", f"gui/{os.getuid()}", path)
    return True
