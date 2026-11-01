"""One desktop app per machine, decided at startup by a lock, not a guess.

THE BUG THIS CLOSES (2026-10-31). A user's scheduled tasks sat in Upcoming for
ever while chat kept working. Their Mac had three `FusedRender` processes:
the one they were using and two leftovers from the night before, each born
a minute before the live app's client reported an outage. `find_running_server`
had asked the live app for `/` with a one-second timeout at the exact moment
it was stalling, read the silence as "no app", and `main()` started a full
second instance — no window, no Dock presence, just a process that parked
its machine-duties waiter behind the live one. When the live app quit, the
leftover inherited the lease that gates every scheduled message, and never
ticked. Every app the user launched after that parked behind it. Chat spawns
Claude directly and needs no lease, which is why chat worked.

**The rule is a flock, held for life.** The first instance to start takes
`instance.lock` in the app-support dir (the same per-branch dir as the
pidfile) and keeps it until the process exits; the OS drops it on any kind
of death. That one fact replaces every heuristic the first cut needed —
"is the pid in the file alive", "did `/` answer in time", "is that sibling
young enough to be booting" — and each of those had a window in which it
killed a healthy app or let a duplicate through (review, 2026-10-31):

* **Won the lock → this process is THE app.** Every other process running
  our exact binary is, by that fact, a leftover: a healthy app would hold
  the lock, a sibling that started first would hold it. `sweep` stops them
  (TERM, wait, KILL). Only our own user's processes, never a zombie, never
  our own process group. One WARNING line + a `stale-app-copies` health
  event; the Diagnostics bundle carries both. Silent for the user.
* **Lost the lock → somebody else is the app.** `defer` waits for that
  holder to serve — up to `BOOT_WAIT_S`, the longest a healthy launch takes
  to write its pidfile (`RELAUNCH_BOOT_WAIT_S`) — polling the pidfile and
  `/`. A holder that exits meanwhile frees the lock and we try to take it.
  The caller then opens the live app and exits for good (`hard_exit`, never
  a bare `return` from `main`, which is how a duplicate lingers). A holder
  that is alive but never serves within the budget is the one case the
  user hears about: an alert naming it, with Quit / Quit that copy and
  start.

**A leftover mid-dispatch loses nothing that matters.** Every Claude run's
host calls `setsid()` the moment it starts (session_host.py), so it is in a
session of its own and `kill_tree`'s `killpg` on the leftover never reaches
it; the run finishes on its own. What dies with the leftover is its watcher
thread, and the next leader's first tick adopts the run from its own files
(`schedule._resolve_orphan` → `_adopt_watch`) and records the verdict.

"Our exact binary" is the full path of our own executable — for the shipped
app `…/FusedRender.app/Contents/MacOS/FusedRender`, as `ps -o comm=` shows
it (the user's own Diagnostics zip listed exactly that for the app and
`…/MacOS/python` for its workers) — realpath'd and compared whole
(`process_executable`, the same rule canvases' force-quit uses). A dev
server (`python -m fused_render.cli`), a lean `fused-render open`, a
Chromium helper or a worker never matches, and neither does a differently
located build of the app. A rebuilt dev bundle at the SAME path holds the
lock while it runs, so a second launch defers to it rather than killing it.

**The lock decides who is the app; the binary only decides who is a
candidate.** Deferring never asks what the holder's executable is — a copy
run from the mounted DMG, a translocated or renamed bundle still holds the
lock and still serves, and must be deferred to. And a rival that holds an
instance lock of ITS OWN — the same binary launched under another
`FUSED_RENDER_BRANCH`, whose lock lives in another branch dir — is another
app, not a leftover: `rivals` probes every instance lock under the support
base and spares a pid that holds one.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field

from fused_render import _platform

logger = logging.getLogger(__name__)

LOCK_NAME = "instance.lock"
#: How long a lock holder gets to start serving before it counts as stuck:
#: a healthy launch writes its pidfile only after the server is ready, which
#: can take the whole readiness cutoff (app.py SERVER_READY_TIMEOUT_S = 60 s),
#: plus the margin the relauncher already allows (RELAUNCH_BOOT_WAIT_S).
BOOT_WAIT_S = 75.0
#: A newcomer always gives the holder at least this long, even one whose
#: record says it started ages ago — a pidfile is written late and read
#: slightly later still.
MIN_WAIT_S = 15.0
POLL_S = 0.5
#: After SIGTERM, how long a leftover gets to go before SIGKILL, then how
#: long SIGKILL gets before the process counts as unkillable. The app has no
#: SIGTERM teardown of its own, so the TERM window is short — it exists for
#: faulthandler's chained handler and for a future graceful quit.
TERM_WAIT_S = 2.0
KILL_WAIT_S = 2.0
PS_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class Rival:
    pid: int
    age_s: float  # seconds since the process started; 0 when unknown


@dataclass
class Sweep:
    killed: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)


@dataclass
class Outcome:
    #: "primary": we hold the lock and are the app. "duplicate": another
    #: process does; `live` is where it serves, or None when it never did
    #: within the budget (`holder` names it for the alert).
    role: str
    live: tuple[int, int] | None = None
    holder: int | None = None
    sweep: Sweep = field(default_factory=Sweep)
    waited_s: float = 0.0


# ----------------------------------------------------------------- the lock

_HANDLE = None  # the open lock file; closing it would release the lock


def lock_path(support_dir: str) -> str:
    return os.path.join(support_dir, LOCK_NAME)


def record_path(support_dir: str) -> str:
    return lock_path(support_dir) + ".json"


def claim(support_dir: str) -> bool:
    """Take the instance lock without waiting. True once this process holds
    it (idempotent). The holder's pid goes into a sidecar — the lock file's
    own bytes are off limits on Windows, where `msvcrt` locks byte 0."""
    global _HANDLE
    if _HANDLE is not None:
        return True
    from fused_render import tasks_store

    handle = tasks_store._open_lockfile(lock_path(support_dir))
    if not _platform.flock(handle, blocking=False):
        handle.close()
        return False
    tmp = record_path(support_dir) + f".{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "since": time.time()}, fh)
        os.replace(tmp, record_path(support_dir))
    except OSError:
        # A lock without a record would be a holder nobody can name: another
        # branch's sweep would read it as a leftover and kill it, and a
        # newcomer could not tell it from a stall. Hold nothing rather than
        # that; the caller treats the raise as "lock dir unusable".
        try:
            os.unlink(tmp)
        except OSError:
            pass
        handle.close()
        raise
    _HANDLE = handle
    return True


def holder(support_dir: str, *, alive=None) -> int | None:
    """The pid the lock's sidecar names, if that process is alive; None
    otherwise (nobody, an older build that wrote no record, a dead pid).
    Deliberately NOT gated on the holder's executable: the lock is held, so
    whoever wrote the record is the app — a copy run from the DMG, a
    translocated or renamed bundle included. Only `rivals` asks about
    binaries."""
    alive_fn = alive_pid if alive is None else alive
    try:
        with open(record_path(support_dir), encoding="utf-8") as fh:
            pid = int(json.load(fh).get("pid") or 0)
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    if pid <= 0 or pid == os.getpid() or not alive_fn(pid):
        return None
    return pid


def holder_since(support_dir: str) -> float | None:
    """When the lock's record says its holder started (epoch seconds), or
    None. The newcomer's wait budget counts from here, not from its own
    start: a holder that began booting a minute ago has a minute less."""
    try:
        with open(record_path(support_dir), encoding="utf-8") as fh:
            since = float(json.load(fh).get("since") or 0)
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    return since if since > 0 else None


def packaged() -> bool:
    """Only a packaged app sweeps. Run from source, "our executable" is a
    Python interpreter, and every dev server, test worker and Claude session
    host on the same interpreter would match it (review, 2026-10-31)."""
    return bool(getattr(sys, "frozen", False))


def _is_interpreter(path: str | None) -> bool:
    name = os.path.basename(path or "").lower()
    return name.startswith("python") or name.startswith("pythonw")


def lock_domains(support_base: str) -> list[str]:
    """Every dir an instance lock can live in under `support_base`: the base
    itself and each `branches/<ref>` beneath it (app.py's APP_SUPPORT_DIR is
    one of these, chosen by FUSED_RENDER_BRANCH)."""
    dirs = [support_base]
    branches = os.path.join(support_base, "branches")
    try:
        for name in sorted(os.listdir(branches)):
            path = os.path.join(branches, name)
            if os.path.isdir(path):
                dirs.append(path)
    except OSError:
        pass
    return dirs


def holds_a_lock(pid: int, support_base: str | None, *, flock=None) -> bool:
    """Whether `pid` holds the instance lock of ANY domain under
    `support_base`: its record names the pid and the lock file refuses a
    non-blocking claim. A fresh handle is used for the probe and closed at
    once, so a lock nobody holds is released the instant it is tested."""
    if not support_base or not os.path.isdir(support_base):
        return False
    if flock is None:
        flock = _platform.flock
    from fused_render import tasks_store

    for domain in lock_domains(support_base):
        try:
            with open(record_path(domain), encoding="utf-8") as fh:
                named = int(json.load(fh).get("pid") or 0)
        except (OSError, ValueError, AttributeError, TypeError):
            continue
        if named != pid:
            continue
        try:
            handle = tasks_store._open_lockfile(lock_path(domain))
        except OSError:
            continue
        try:
            if not flock(handle, blocking=False):
                return True
        finally:
            handle.close()
    return False


def release_for_tests() -> None:
    global _HANDLE
    if _HANDLE is not None:
        try:
            _HANDLE.close()
        except OSError:
            pass
        _HANDLE = None


# ------------------------------------------------------------ who else is us


def _same_binary(a: str | None, b: str | None) -> bool:
    """Same executable, by real path."""
    if not a or not b:
        return False
    try:
        a = os.path.realpath(a)
        b = os.path.realpath(b)
    except OSError:
        return False
    return os.path.normcase(a) == os.path.normcase(b)


def own_executable(processes: list[tuple] | None = None) -> str | None:
    """Our binary's path: from the same `ps` listing when the caller has one
    (macOS `comm` is the full path), else `process_executable`."""
    if processes:
        for row in processes:
            if row[0] == os.getpid() and row[-1] and os.path.isabs(row[-1]):
                return row[-1]
    from fused_render import canvas_sync_state

    return canvas_sync_state.process_executable(os.getpid()) or sys.executable or None


def is_our_binary(pid: int, own: str | None = None) -> bool:
    from fused_render import canvas_sync_state

    return _same_binary(canvas_sync_state.process_executable(pid), own or own_executable())


def parse_etime(text: str) -> float:
    """`ps` elapsed time (`[[dd-]hh:]mm:ss`) in seconds; 0 when unreadable —
    "unknown age" must never read as "ancient"."""
    text = text.strip()
    try:
        days = 0
        if "-" in text:
            d, text = text.split("-", 1)
            days = int(d)
        parts = [int(p) for p in text.split(":")]
    except ValueError:
        return 0.0
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, (m, s) = 0, parts
    else:
        return 0.0
    return days * 86400 + h * 3600 + m * 60 + s


def list_processes(*, run=subprocess.run) -> list[tuple[int, int, float, str, str]]:
    """`(pid, uid, age_s, stat, command)` for every process, from one `ps`.
    `command` is `ps -o comm=` — the full path on macOS, a short name on
    Linux, which is why `rivals` confirms with `process_executable`. Empty
    on Windows (no `ps`; the Windows app has its own single-instance story)
    and on any failure: a sweep that cannot see does nothing."""
    if _platform.IS_WINDOWS:
        return []
    try:
        result = run(["/bin/ps", "-axo", "pid=,uid=,etime=,stat=,comm="],
                     capture_output=True, text=True, encoding="utf-8", errors="replace",
                     timeout=PS_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in (result.stdout or "").splitlines():
        parts = line.strip().split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid, uid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        rows.append((pid, uid, parse_etime(parts[2]), parts[3], parts[4]))
    return rows


def rivals(*, own_pid: int | None = None, own_uid: int | None = None,
           own_pgid: int | None = None, own_exe: str | None = None,
           processes=None, executable_of=None, support_base: str | None = None,
           holds_lock=None) -> list[Rival]:
    """Every OTHER live process of OUR user running our executable that
    holds no instance lock of its own. Zombies are not processes; another
    user's app is not ours to touch; the leader of our own process group is
    never a target (`killpg` on it is us); a pid holding a lock in another
    domain under `support_base` is another app, not a leftover."""
    if holds_lock is None:
        holds_lock = holds_a_lock
    if not packaged() and own_exe is None:
        return []
    own_pid = os.getpid() if own_pid is None else own_pid
    own_uid = _platform.euid() if own_uid is None else own_uid
    if own_pgid is None:
        try:
            own_pgid = os.getpgid(0)
        except (OSError, AttributeError):
            own_pgid = -1
    rows = list_processes() if processes is None else processes
    own_exe = own_executable(rows) if own_exe is None else own_exe
    if not own_exe or _is_interpreter(own_exe):
        return []
    if executable_of is None:
        from fused_render import canvas_sync_state

        executable_of = canvas_sync_state.process_executable
    own_name = os.path.basename(os.path.normcase(own_exe))
    found: list[Rival] = []
    for pid, uid, age, stat, command in rows:
        if pid in (own_pid, own_pgid) or pid <= 0 or uid != own_uid:
            continue
        if stat.startswith("Z"):
            continue
        name = os.path.basename(os.path.normcase(command.strip()))
        # Linux's `comm` is cut at 15 characters; a prefix match keeps it in.
        if not (name == own_name or (len(name) >= 15 and own_name.startswith(name))):
            continue
        if not _same_binary(executable_of(pid), own_exe):
            continue
        if support_base and holds_lock(pid, support_base):
            logger.info("single instance: pid %s runs this binary under another branch "
                        "and holds its own lock; left alone", pid)
            continue
        found.append(Rival(pid=pid, age_s=age))
    return sorted(found, key=lambda r: r.pid)


# ---------------------------------------------------------- liveness probes


def alive_pid(pid: int) -> bool:
    """Exists and is not a zombie. A pid we may not signal exists."""
    try:
        _platform.probe_pid(pid)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001 — OverflowError and friends
        return False
    return not _zombie(pid)


alive = alive_pid  # the name the rest of this module and app.py use


def _zombie(pid: int) -> bool:
    if _platform.IS_WINDOWS:
        return False
    try:
        out = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=PS_TIMEOUT_S, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return out.strip().startswith("Z")


def _probe_ok(port: int) -> bool:
    """A fused-render server answers `/api/config` with JSON that carries a
    `version` — a bare 200 from `/` would accept any local HTTP server that
    happened to take the port after a crash."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=1) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read(65536).decode("utf-8", "replace") or "{}")
    except Exception:  # noqa: BLE001 — refused, timeout, bad status, not JSON: not ok
        return False
    return isinstance(data, dict) and bool(data.get("version"))


# --------------------------------------------------------------- the sweep


def sweep(found: list[Rival], *, alive=alive, term=None, kill=None, still_rival=None,
          term_wait_s: float = TERM_WAIT_S, kill_wait_s: float = KILL_WAIT_S,
          clock=time.monotonic, sleep=time.sleep) -> Sweep:
    """Stop every rival: TERM, wait, KILL, wait. What is still alive after
    that is `failed`. `still_rival(pid)` is asked immediately before EACH
    signal — a rival that exited and had its pid reused between the listing
    and the signal must not take the signal; default: the pid still runs our
    binary."""
    if term is None:
        def term(pid: int) -> None:
            _platform.kill_tree(pid, signal.SIGTERM)
    if kill is None:
        def kill(pid: int) -> None:
            _platform.kill_tree(pid, _platform.SIGKILL)
    if still_rival is None:
        still_rival = is_our_binary
    result = Sweep()
    targets = []
    for rival in found:
        if still_rival(rival.pid):
            targets.append(rival.pid)
        else:
            logger.info("single instance: pid %s is no longer this app; left alone", rival.pid)
    if not targets:
        return result
    for pid in targets:
        try:
            term(pid)
        except Exception:  # noqa: BLE001 — gone already, or not ours
            logger.debug("single instance: TERM %s failed", pid, exc_info=True)
    pending = _wait_gone(targets, alive, term_wait_s, clock, sleep)
    for pid in pending:
        if not still_rival(pid):
            continue  # died and was reused in the wait: not ours to KILL
        try:
            kill(pid)
        except Exception:  # noqa: BLE001
            logger.debug("single instance: KILL %s failed", pid, exc_info=True)
    if pending:
        pending = _wait_gone(pending, alive, kill_wait_s, clock, sleep)
    result.failed = pending
    result.killed = [pid for pid in targets if pid not in pending]
    return result


def _wait_gone(pids: list[int], alive, wait_s: float, clock, sleep) -> list[int]:
    deadline = clock() + wait_s
    left = list(pids)
    while left and clock() < deadline:
        left = [pid for pid in left if alive(pid)]
        if left:
            sleep(0.2)
    return [pid for pid in left if alive(pid)]


# -------------------------------------------------------------- all together


def enforce(support_dir: str, read_pidfile, *, support_base: str | None = None,
            claim=claim, holder=holder, holder_since=holder_since, is_ours=None,
            rivals_of=None, sweep_with=sweep, probe=_probe_ok, alive=alive,
            boot_wait_s: float = BOOT_WAIT_S, min_wait_s: float = MIN_WAIT_S,
            poll_s: float = POLL_S, clock=time.monotonic, wall=time.time,
            sleep=time.sleep, record=None) -> Outcome:
    """The whole startup decision. `read_pidfile()` answers `(pid, port)` or
    `(None, None)` from the pidfile/portfile the serving app writes once it
    is ready. `support_base` is the unbranched support dir, under which
    every branch's instance lock lives (`rivals` spares their holders).
    `is_ours(pid)` decides whether a pidfile pid that is NOT the lock's
    recorded holder may be deferred to: only if it runs our binary (a crash
    can leave a pidfile whose pid was recycled by something else listening
    on the port).
    `record(kind, **fields)` is `health.record_event` when the caller has
    one."""
    if is_ours is None:
        is_ours = is_our_binary
    if rivals_of is None:
        def rivals_of():
            return rivals(support_base=support_base)
    ours_cache: dict[int, bool] = {}

    def pidfile_is_the_app(pid: int, who: int | None) -> bool:
        # The lock's record names the app; a pidfile pid that is not it has
        # to prove itself by its binary (a crash leaves a pidfile; the pid can
        # be reused by something else that listens on the port). `claim`
        # never holds a lock without a record, so "held, no record" means
        # the record is momentarily unreadable — the same proof applies.
        if who is not None and pid == who:
            return True
        if pid not in ours_cache:
            ours_cache[pid] = bool(is_ours(pid))
        return ours_cache[pid]

    if claim(support_dir):
        return Outcome(role="primary", sweep=sweep_leftovers(rivals_of, sweep_with, record))
    started = clock()

    def budget_for_holder() -> float:
        # The holder's boot budget counts from ITS start when the record says
        # when that was, never less than `min_wait_s` and never more than a
        # whole budget from now.
        since = holder_since(support_dir)
        if since is None:
            return boot_wait_s
        return min(boot_wait_s, max(min_wait_s, since + boot_wait_s - wall()))

    who = holder(support_dir)
    deadline = clock() + budget_for_holder()
    while True:
        pid, port = read_pidfile()
        if pid and port and alive(pid) and pidfile_is_the_app(pid, who) and probe(port):
            return Outcome(role="duplicate", live=(pid, port), holder=who,
                           waited_s=clock() - started)
        if who is not None and not alive(who):
            who = None
        if who is None and claim(support_dir):
            # The holder went away (or never wrote a record and has now
            # exited): the lock is ours after all.
            out = Outcome(role="primary",
                          sweep=sweep_leftovers(rivals_of, sweep_with, record))
            out.waited_s = clock() - started
            return out
        if who is None:
            who = holder(support_dir)
            if who is not None:
                # A NEW holder (the old one died, a successor took the lock):
                # its own budget, from its own start — not the tail of the
                # old one's.
                deadline = clock() + budget_for_holder()
        if clock() >= deadline:
            return Outcome(role="duplicate", live=None, holder=who,
                           waited_s=clock() - started)
        sleep(poll_s)


def sweep_leftovers(rivals_of=None, sweep_with=sweep, record=None,
                    support_base: str | None = None) -> Sweep:
    """Rule 2 on its own: stop every other copy of our binary. For the lock
    winner — `enforce` — and for the moment the user consents to "Stop it
    and start" (app.py), which has to clear every leftover, not only the
    one that held the lock."""
    if rivals_of is None:
        def rivals_of():
            return rivals(support_base=support_base)
    found = rivals_of()
    result = sweep_with(found)
    if result.killed or result.failed:
        one = len(found) == 1
        logger.warning(
            "single instance: %d other %s of this app %s running without the "
            "instance lock — leftover(s) from an earlier launch; stopped %s%s. "
            "Scheduled tasks would have waited behind them.",
            len(found), "copy" if one else "copies", "was" if one else "were",
            result.killed, f"; could not stop {result.failed}" if result.failed else "")
        if record is not None:
            try:
                record("stale-app-copies", killed=result.killed, failed=result.failed)
            except Exception:  # noqa: BLE001 — a record never blocks a launch
                logger.debug("single instance: record failed", exc_info=True)
    return result
