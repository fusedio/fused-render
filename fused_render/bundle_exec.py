"""Can the packaged interpreter actually be spawned? Evidence, repair, probe.

Why this exists (2026-10-23, a 0.6.84 install on macOS 26.6 reached through
the in-app updater): six consecutive boots logged

    PermissionError: [Errno 13] Permission denied:
        '/Applications/FusedRender.app/Contents/MacOS/python'

from every `posix_spawn` of `sys.executable` — the Claude agent, the index
workers, the engine warm-up, the PYTHONHOME wrapper's `exec` (bash 3.2 there
printed "Permission denied", the branch it takes when `stat` says the file is
not executable, not "cannot execute binary file"). The app itself booted
fine every time: a py2app launcher never execs `Contents/MacOS/python`, it
only points `sys.executable` at it, so one non-spawnable file inside the
bundle leaves the window up and every feature behind it dead. The same DMG
swapped in by the same `ditto` on another Mac came out `-rwxr-xr-x` and
spawns, so the artifact is fine and the cause is on the affected machine;
nothing in its diagnostics bundle said whether the mode bits were wrong or
the kernel refused a correct file. Hence three pieces, used by three callers:

* `describe(path)` — the evidence: mode, owner, xattr names, `access(X_OK)`.
  Logged BEFORE any repair so the app log alone settles the question next
  time, and written into the diagnostics bundle (`os/bundle-exec.txt`).
* `repair_exec_bits(contents)` — the one repair that is ours to make: set
  `0o755` onto the regular files in `Contents/MacOS` and
  `Contents/Resources/bin`. chmod does not touch the Developer ID seal
  (signatures cover content, not mode); ownership and ACLs are out of scope.
* `probe(contents)` — the assertion: spawn the bundle's python with the
  PYTHONHOME it needs and see it run. `access(X_OK)` is not this test — the
  kernel can pass it and still refuse the exec.

Callers: `update/mac.py` proves a freshly staged bundle BEFORE the swap and
refuses to install one whose python will not spawn (the user keeps the app
that works); `app.py` repairs its own bundle at boot, before anything spawns;
`diagnostics.py` records the state of the bundle it runs from.

Stdlib only, no AppKit, no fused_render imports at module load: the updater
and the boot path both run it before the server exists.
"""
from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys

logger = logging.getLogger("fused_render.bundle_exec")

PROBE_TIMEOUT_S = 60.0
_PROBE = "import sys; print(sys.prefix)"
# What the probe child must NOT inherit: the same set the compute backend and
# the install worker strip from their children (engine._child_env), plus the
# two the framework launcher exports to point a child back at itself.
_STRIPPED = ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV",
             "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__")
# The directories the swap writes executables into (build_dmg.sh): the
# interpreter and the Swift helper, and the shell wrappers under Resources/bin.
_EXEC_DIRS = (("MacOS",), ("Resources", "bin"))


def interpreter(contents: str) -> str:
    """`<Contents>/MacOS/python` — where py2app puts the real interpreter."""
    return os.path.join(contents, "MacOS", "python")


def own_contents() -> str | None:
    """`Contents/` of the bundle this process runs from, or None when not
    packaged (the same anatomy app.bundle_path reads; duplicated so this
    module imports nothing)."""
    if sys.platform != "darwin" or getattr(sys, "frozen", None) != "macosx_app":
        return None
    return os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))


def describe(path: str) -> str:
    """One line of facts about `path`, for a log or a diagnostics file."""
    try:
        st = os.lstat(path)
    except OSError as e:
        return f"{path}: {type(e).__name__}: {e}"
    # `os.listxattr` is Linux-only; macOS lists them through /usr/bin/xattr.
    try:
        r = subprocess.run(["/usr/bin/xattr", path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=5, check=False)
        names = sorted(ln.strip() for ln in r.stdout.splitlines() if ln.strip())
        xattrs = ",".join(names) if names else ("-" if r.returncode == 0 else f"(exit {r.returncode})")
    except (OSError, subprocess.SubprocessError) as e:  # noqa: BLE001 - informational
        xattrs = f"({type(e).__name__})"
    return (f"{path}: mode={stat.filemode(st.st_mode)} ({oct(st.st_mode & 0o7777)}) "
            f"uid={st.st_uid} gid={st.st_gid} size={st.st_size} "
            f"xattrs={xattrs} access_x={os.access(path, os.X_OK)} "
            f"euid={getattr(os, 'geteuid', lambda: '?')()} "
            f"egid={getattr(os, 'getegid', lambda: '?')()}")


def repair_exec_bits(contents: str) -> tuple[list[str], list[str]]:
    """Set 0o755 onto every regular file under the bundle's executable
    directories that lacks any of it. Returns (repaired, failed) paths. Never
    raises: a root-owned file fails EPERM here and that is a fact to log,
    not a reason to stop a boot or an install."""
    repaired: list[str] = []
    failed: list[str] = []
    for parts in _EXEC_DIRS:
        d = os.path.join(contents, *parts)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            p = os.path.join(d, name)
            try:
                st = os.lstat(p)
                if not stat.S_ISREG(st.st_mode):
                    continue  # symlinks and dirs are not what fails to spawn
                want = st.st_mode | 0o755
                if want == st.st_mode:
                    continue
                os.chmod(p, stat.S_IMODE(want))
            except OSError as e:
                failed.append(f"{p} ({type(e).__name__}: {e})")
                continue
            repaired.append(p)
    return repaired, failed


def probe(contents: str, timeout: float = PROBE_TIMEOUT_S) -> tuple[str | None, bool]:
    """Spawn `<contents>/MacOS/python` and have it report its prefix.
    (reason, conclusive): reason is None when it ran and answered with this
    bundle's Resources, else one line saying why not. `conclusive` is False
    when the failure says nothing about the file itself — a timeout (a first
    exec of a fresh copy can sit in Gatekeeper's assessment on a loaded Mac)
    or a child killed by a signal — so a caller never refuses an install on
    a verdict the probe did not actually reach.

    PYTHONHOME is set explicitly to the bundle's own Resources — a py2app
    interpreter only finds its runtime through it (engine.py's wrapper exists
    for exactly that reason), and without it the probe would fail for a
    reason that has nothing to do with the exec. No `-I`: it implies `-E`,
    which ignores PYTHONHOME.
    """
    exe = interpreter(contents)
    home = os.path.join(contents, "Resources")
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED}
    env["PYTHONHOME"] = home
    try:
        # cwd "/" : `-c` puts the cwd on sys.path, and the probe must not run a
        # sitecustomize planted wherever the caller happens to be.
        proc = subprocess.run(
            [exe, "-c", _PROBE], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, env=env, check=False, cwd="/",
        )
    except subprocess.TimeoutExpired:
        return f"no answer within {timeout:g}s", False
    except (OSError, subprocess.SubprocessError) as e:
        return f"{type(e).__name__}: {e}", True
    if proc.returncode < 0:
        return f"killed by signal {-proc.returncode}", False
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return f"exit {proc.returncode}: {tail[-1] if tail else 'no output'}", True
    got = (proc.stdout or "").strip().splitlines()
    if not got or os.path.realpath(got[-1]) != os.path.realpath(home):
        return f"reported prefix {got[-1] if got else '(nothing)'!r}, expected {home!r}", True
    return None, True


def ensure_spawnable(contents: str, *, what: str) -> str | None:
    """Prove the bundle's python spawns; repair the mode bits and prove it
    again when it does not. Returns None when it spawns (possibly after the
    repair) — or when the probe never reached a verdict, which is logged but
    is not a reason to refuse — else the final, definite reason. Logs the
    evidence first, every time."""
    reason, conclusive = probe(contents)
    if reason is None:
        return None
    exe = interpreter(contents)
    if not conclusive:
        logger.warning("%s: %s gave no verdict (%s); not held against it. %s",
                       what, exe, reason, describe(exe))
        return None
    logger.warning("%s: %s cannot be spawned (%s); %s", what, exe, reason, describe(exe))
    repaired, failed = repair_exec_bits(contents)
    if repaired:
        logger.warning("%s: set 0755 on %d file(s): %s", what, len(repaired), ", ".join(repaired))
    if failed:
        logger.warning("%s: could not chmod %d file(s): %s", what, len(failed), "; ".join(failed))
    again, conclusive = probe(contents)
    if again is None:
        logger.warning("%s: %s spawns after the repair", what, exe)
        return None
    if not conclusive:
        logger.warning("%s: %s gave no verdict after the repair (%s); not held "
                       "against it", what, exe, again)
        return None
    logger.error("%s: %s still cannot be spawned after the repair (%s); %s",
                 what, exe, again, describe(exe))
    return again


def repair_own_bundle() -> None:
    """Boot-time, best-effort: if this process's own interpreter is not
    executable by mode bits, say so with the evidence and set the bits before
    anything spawns it. A plain `access` check, not a spawn — this runs on the
    startup path and the engine warm-up spawns it for real moments later; what
    matters here is that the log carries the file's state and that the one
    repair we can make has been made. Never raises."""
    try:
        contents = own_contents()
        if contents is None:
            return
        exe = interpreter(contents)
        if os.access(exe, os.X_OK):
            return
        logger.warning("boot: own interpreter is not executable; %s", describe(exe))
        repaired, failed = repair_exec_bits(contents)
        if repaired:
            logger.warning("boot: set 0755 on %d file(s): %s", len(repaired), ", ".join(repaired))
        if failed:
            logger.warning("boot: could not chmod %d file(s): %s", len(failed), "; ".join(failed))
        logger.warning("boot: after repair, %s", describe(exe))
    except Exception:  # noqa: BLE001 - a diagnostics aid never stops a launch
        logger.warning("boot: own-interpreter repair failed", exc_info=True)
