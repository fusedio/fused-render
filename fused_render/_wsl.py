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
# The opener script `route_children` writes, and the name it is also linked under.
OPENER_NAME = "fused-render-open"
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


def browser_chosen(env=None) -> bool:
    """A `BROWSER` the person set that should stay in charge. One that names only `explorer.exe` does not
    count: it is the very route that shows the Documents folder for a sign-in address."""
    value = (os.environ if env is None else env).get("BROWSER") or ""
    names = [entry.split()[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
             for entry in value.split(os.pathsep) if entry.strip()]
    # Our own opener (route_children) is not the person's choice either: it is this module's route.
    return any(name not in ("explorer.exe", "explorer", OPENER_NAME) for name in names)


class _WindowsBrowser(webbrowser.BaseBrowser):
    def open(self, url, new=0, autoraise=True):
        return open_url(url)


def use_windows_browser() -> bool:
    """Under WSL, make every `webbrowser.open` in this process try the Windows default browser first (what it
    cannot take falls through to the usual xdg-open). A `BROWSER` the person set stays in charge, unless it
    is `explorer.exe` (browser_chosen). Safe to call more than once. True when it is in place."""
    global _registered
    if not is_wsl() or browser_chosen():
        return False
    if _registered:
        return True
    _registered = True
    webbrowser.register(BROWSER_NAME, None, _WindowsBrowser(BROWSER_NAME), preferred=True)
    return True


def opener_script(rundll32, real_xdg_open) -> str:
    """The shell script behind `route_children`: an http(s) address goes to the Windows default browser,
    anything else to the real `xdg-open` (or fails like a missing one, exit 3)."""
    fallback = f'exec "{real_xdg_open}" "$@"' if real_xdg_open else "exit 3"
    return (
        "#!/bin/sh\n"
        "# Written by fused-render (_wsl.py). Under WSL, xdg-open hands a web address to explorer.exe, which\n"
        "# shows a folder for any address with a query. This hands it to the Windows default browser.\n"
        'case "$1" in\n'
        f'  http://*|https://*|HTTP://*|HTTPS://*) exec "{rundll32}" url.dll,FileProtocolHandler "$1" ;;\n'
        "esac\n"
        f"{fallback}\n"
    )


def route_children(bin_dir, env=None, which=shutil.which, exists=os.path.exists) -> bool:
    """Under WSL, make the programs this process SPAWNS open web addresses in the Windows browser too.

    `use_windows_browser` covers only Python's `webbrowser` in this process. A sign-in run by a CLI child
    (`claude auth login`, `gh auth login`, the other agents') opens its page itself, through `xdg-open` or
    `$BROWSER`, and so still ended in `explorer.exe <url>`: a folder window instead of the sign-in page.
    This writes one opener into `bin_dir` under two names, `xdg-open` and OPENER_NAME, puts `bin_dir` first
    on PATH and names the opener in BROWSER, so both routes reach it. A BROWSER the person set is left
    alone (browser_chosen). Never raises; False when not WSL, no rundll32, or the files cannot be written."""
    env = os.environ if env is None else env
    if not is_wsl(env):
        return False
    rundll32 = windows_exe("rundll32.exe", which, exists)
    if not rundll32:
        return False
    parts = [p for p in (env.get("PATH") or "").split(os.pathsep) if p and p != bin_dir]
    # The real one, looked up WITHOUT our own dir, so the opener never calls itself.
    real = which("xdg-open", path=os.pathsep.join(parts))
    script = opener_script(rundll32, real)
    try:
        os.makedirs(bin_dir, exist_ok=True)
        for name in ("xdg-open", OPENER_NAME):
            path = os.path.join(bin_dir, name)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(script)
            os.chmod(tmp, 0o755)
            os.replace(tmp, path)
    except OSError:
        return False
    env["PATH"] = os.pathsep.join([bin_dir, *parts])
    if not browser_chosen(env):
        env["BROWSER"] = os.path.join(bin_dir, OPENER_NAME)
    return True
