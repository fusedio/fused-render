"""WSL: a Linux server whose desktop (and default browser) is on the Windows side.

Two things differ from plain Linux, both about that split:

- Opening a web address. `webbrowser.open` and `xdg-open` both end in `explorer.exe <url>` under WSL, and
  Explorer takes a URL whose query has an `=` for a path it cannot find: it opens the Documents folder
  instead of the browser (xdg-utils issue 242). A plain `http://127.0.0.1:1777/` gets through, which is why
  the app's own page opened while every sign-in address (Auth0's authorize URL, an OAuth connect) did not.
  `rundll32.exe url.dll,FileProtocolHandler <url>` hands the address to the Windows default browser whole;
  no shell reads it, so `&` and `=` need no quoting.
- Becoming root. `wsl.exe -u root` starts a root process in the same distro with no password: the Windows
  account owns the distro. That is what lets a button install a package (bots/chrome_install.py).

Stdlib only: the supervisor and the `_fused_login.py` child import it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser

# Where the Windows tools sit when the distro does not append the Windows PATH (`appendWindowsPath=false`).
WINDOWS_SYSTEM32 = "/mnt/c/Windows/System32"
BROWSER_NAME = "fused-render-wsl"
_registered = False


def is_wsl(env=None, release=None, platform=None) -> bool:
    """Linux under WSL (1 or 2). Pure when every argument is passed."""
    if not (sys.platform if platform is None else platform).startswith("linux"):
        return False
    env = os.environ if env is None else env
    release = os.uname().release if release is None else release
    return bool(env.get("WSL_DISTRO_NAME")) or "microsoft" in release.lower()


def windows_exe(name, which=shutil.which, exists=os.path.exists):
    """Path of a Windows system tool (`rundll32.exe`, `wsl.exe`) as seen from Linux, or None."""
    found = which(name)
    if found:
        return found
    fallback = f"{WINDOWS_SYSTEM32}/{name}"
    return fallback if exists(fallback) else None


def url_argv(url, which=shutil.which, exists=os.path.exists):
    """The command that opens `url` in the Windows default browser, or None: only http(s) goes this way
    (FileProtocolHandler would run a file or any registered scheme), and only when rundll32 is reachable."""
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        return None
    exe = windows_exe("rundll32.exe", which, exists)
    return [exe, "url.dll,FileProtocolHandler", url] if exe else None


def open_url(url) -> bool:
    """Open `url` in the Windows default browser. False = not handled (not http(s), no rundll32, or Windows
    interop is off and the .exe will not start): the caller falls back to what it did before."""
    argv = url_argv(url)
    if not argv:
        return False
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True


class _WindowsBrowser(webbrowser.BaseBrowser):
    def open(self, url, new=0, autoraise=True):
        return open_url(url)


def use_windows_browser() -> bool:
    """Under WSL, make every `webbrowser.open` in this process try the Windows default browser first (what it
    cannot take falls through to the usual xdg-open). A `BROWSER` the person set stays in charge. Safe to call
    more than once. True when it is in place."""
    global _registered
    if not is_wsl() or os.environ.get("BROWSER"):
        return False
    if _registered:
        return True
    _registered = True
    webbrowser.register(BROWSER_NAME, None, _WindowsBrowser(BROWSER_NAME), preferred=True)
    return True
