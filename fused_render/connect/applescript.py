"""Apple apps on this Mac as connected services: the `local-app` sign-in kind
and the `osascript` runner the Apple packs' tools share (SPEC-connect-oauth §2).

Notes.app and Calendar.app already hold the user's accounts, so there is no
token, no OAuth and nothing to refresh: "Connect" is one harmless read
against the app (the probe), which is what makes macOS ask once whether
Fused may control it (System Settings > Privacy & Security > Automation).
The record stores the kind, the app and an account label, never a credential.

    available()              (ok, reason) — macOS with `osascript` on PATH, or why not
    Probe(app, account_js)   start() / result(timeout) / cancel(): the consent probe on
                             its own process, cancellable like a browser flow
    run_jxa(script, args)    one JavaScript-for-Automation program -> its JSON result;
                             raises AutomationDenied (-1743: the grant is missing, the
                             card says Reconnect) or AutomationError (anything else,
                             the app's own last stderr line). A bare `{"error"}` the
                             script returns is raised too; one with more keys beside
                             it (candidates to pick from) is returned as the answer.

`-1743` is "Not authorized to send Apple events": macOS refused, or the user
clicked Don't Allow, or the prompt went unanswered until the timeout. The
packaged app can only get the prompt because its Info.plist declares
`NSAppleEventsUsageDescription` (scripts/setup_py2app.py); a bundle without it
is refused silently, which is what the Apple Notes starter's NotesBridge applet
worked around. Here the host app is the one asking, by design: one grant, one
name in the Automation list.

User text is passed as argv to the script, never interpolated into it, the
mac-automation starter's rule. Nothing here logs a result body: a note is the
user's data.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)

KIND = "local-app"
TOOL_TIMEOUT = 55.0
#: The consent probe may sit behind the macOS prompt until the user answers it.
PROBE_TIMEOUT = 180.0
DENIED_HOWTO = ("macOS has not let Fused control {app}. Open System Settings > Privacy & Security > "
                "Automation, turn on {app} under Fused, then Reconnect.")


class AutomationError(Exception):
    """The app or osascript said no; the message is their own last line."""


class AutomationDenied(AutomationError):
    """-1743: this process may not send Apple events to the app."""


def available(platform: str | None = None) -> tuple[bool, str | None]:
    """Static: is this a Mac with `osascript`? (Whether the user has granted
    access is the probe's business, at Connect time, not a listing's.)"""
    if (platform or sys.platform) != "darwin":
        return False, "only on a Mac: it talks to the app installed there"
    if not shutil.which("osascript"):
        return False, "`osascript` is not installed"
    return True, None


def _denied(err: str) -> bool:
    low = (err or "").lower()
    return "-1743" in low or "not authorized" in low or "not allowed" in low


def _classify(rc: int, stdout: str, stderr: str, app: str) -> Exception:
    err = (stderr or stdout or "").strip()
    if _denied(err):
        return AutomationDenied(DENIED_HOWTO.format(app=app))
    if "-600" in err or "isn't running" in err.lower():
        return AutomationError(f"{app} could not be launched")
    if "-1712" in err:
        return AutomationError(f"{app} did not answer in time (is it showing a dialog?)")
    last = (err.splitlines() or [f"osascript exit {rc}"])[-1]
    return AutomationError(last[:600])


def _parse(stdout: str, app: str) -> dict:
    try:
        out = json.loads((stdout or "").strip() or "{}")
    except ValueError:
        raise AutomationError(f"{app} returned something that is not JSON") from None
    if not isinstance(out, dict):
        raise AutomationError(f"{app} returned something that is not an object")
    if isinstance(out.get("error"), str) and out["error"]:
        if _denied(out["error"]):
            raise AutomationDenied(DENIED_HOWTO.format(app=app))
        if len(out) == 1:
            raise AutomationError(out["error"][:600])
        # An error with company (Notes' append: `candidates` for an ambiguous
        # title) is an answer the caller needs whole: returned, not raised.
    return out


def run_jxa(script: str, args: list | tuple = (), app: str = "the app", timeout: float = TOOL_TIMEOUT) -> dict:
    """Run one JXA program (`function run(argv)` returning `JSON.stringify(...)`).

    `args` go on argv as strings. Returns the parsed object; a `{"error": ...}`
    the script returns is raised too, so a tool's handler sees one shape.
    """
    ok, why = available()
    if not ok:
        raise AutomationError(why or "not available")
    cmd = ["osascript", "-l", "JavaScript", "-", *[str(a) for a in args]]
    try:
        r = subprocess.run(cmd, input=script, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, stdin=None)
    except subprocess.TimeoutExpired:
        raise AutomationError(f"{app} did not answer within {int(timeout)} s") from None
    except OSError as e:
        raise AutomationError(f"osascript could not run: {e.__class__.__name__}") from None
    if r.returncode != 0:
        raise _classify(r.returncode, r.stdout, r.stderr, app)
    return _parse(r.stdout, app)


_PROBE_JS = r"""
function run(argv) {
  const app = Application(argv[0]);
  app.includeStandardAdditions = false;
  const name = app.name();
  let account = "";
  try { account = String(eval(argv[1] || '""') || ""); } catch (e) { account = ""; }
  return JSON.stringify({ name: name, account: account });
}
"""


class Probe:
    """The consent probe: `app.name()` plus the pack's account expression, on
    a process the manager can cancel (Cancel on the card kills it, which also
    dismisses nothing: the macOS prompt stays until the user answers, and the
    next Connect simply finds the grant made or refused)."""

    def __init__(self, app: str, account_js: str = ""):
        self.app = app
        self.account_js = account_js or ""
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._cancelled = False

    def start(self) -> None:
        ok, why = available()
        if not ok:
            raise AutomationError(why or "not available")
        cmd = ["osascript", "-l", "JavaScript", "-", self.app, self.account_js]
        with self._lock:
            if self._cancelled:
                return
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")

    def result(self, timeout: float = PROBE_TIMEOUT) -> dict:
        """{"name", "account"}; raises AutomationDenied / AutomationError."""
        with self._lock:
            p = self._proc
        if p is None:
            raise AutomationError("cancelled")
        try:
            out, err = p.communicate(_PROBE_JS, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.cancel()
            raise AutomationError(f"{self.app} did not answer within {int(timeout)} s. If macOS is asking "
                                  f"whether Fused may control {self.app}, click OK and try again.") from None
        if self._cancelled:
            raise AutomationError("cancelled")
        if p.returncode != 0:
            raise _classify(p.returncode, out, err, self.app)
        return _parse(out, self.app)

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            p = self._proc
        if p is not None and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass
