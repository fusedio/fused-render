"""Detached worker that builds one script venv, spawned by envinstall.start().

Run as:  python _env_install_worker.py <key> <progress_dir> <venvs_path>
                                      <python_executable> <req>...

`<python_executable>` is the base interpreter the venv is built from, and it must
be the value `envinstall._python_executable()` returned — `python_identity` folds
it into the venv key, so a different one here builds a venv the server never
looks for and `is_installed()` never turns true. argv cannot carry None, so the
EMPTY STRING stands for "the backend's default"; `main` is the one place that
mapping happens.

Reports through `<progress_dir>/progress.json` — the same
`{stage, pct, detail, done, error, pid, ts}` record
`fused_render/templates/docs/install_worker.py` writes for the typst download,
so the page shell polls one shape.

Two deliberate choices:

**It builds through `fused`'s `ensure_requirements_venv`, not its own uv
commands.** That function owns the ready marker, the half-built-directory
rebuild and the disk-quota diagnostics; a second implementation here would be a
second thing to keep correct, and — worse — could disagree with the venv key the
run then looks for.

**Its error text is upstream's, unedited.** `venvs._run_step` raises
`RuntimeError("Failed to <step>:\\n<stderr>")` with uv's or pip's own stderr in
it. That string goes into `progress.json` verbatim, because a resolver failure
("no wheels with a matching platform tag for imagecodecs") is the actual answer
the user needs — the whole reason this install is a visible flow instead of a
30-second timeout inside /api/run.

Stdlib + `fused` only: no `fused_render` import. It runs on whatever
`sys.executable` the server used, which is also the interpreter the venv keys
on, so what it can import is what the server could.
"""
import json
import os
import sys
import threading
import time

# Stages and their percentages, kept in step with fused_render/envinstall.py's
# STAGES/STAGE_PCT. Duplicated rather than imported because this file must stay
# importable-free of the package (it is spawned as a plain script, and
# `import fused_render` in a detached child is exactly the bootstrap that broke
# once already — see D152).
_CREATE_PCT = 10
_INSTALL_PCT = 25

# How often the install stage re-writes its record while uv runs (D213). The whole
# download happens inside ONE `ensure_requirements_venv` call behind
# `capture_output=True`, so nothing about its internals is observable from here;
# what this buys is proof of LIFE. The client polls every 500ms, so ~2s is well
# under the rate at which a repaint could look stale, and it is four orders of
# magnitude cheaper than the download it reports on.
_HEARTBEAT_S = 2

# How long the terminal write waits for the heartbeat to stop. Generous relative to
# the beat (which wakes immediately on the Event) because the only thing it protects
# against is a beat parked inside its own `_write`; the latch in `install` is what
# makes correctness independent of this number.
_HEARTBEAT_JOIN_S = 5


def _write(progress_dir, stage, pct, detail="", done=False, error=None):
    # Unique temp name, not a shared `progress.json.tmp`: the server writes this
    # same file (envinstall._write) and two writers racing on one temp means the
    # first os.replace consumes the second's file, whose replace then fails.
    #
    # Pid AND thread id, matching `envinstall._write`. The pid alone stopped being
    # unique when the heartbeat arrived: two writers now live in THIS process, and
    # a shared temp name between them is the same race with the same outcome — a
    # crashed installer whose venv was actually built fine.
    path = os.path.join(progress_dir, "progress.json")
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"stage": stage, "pct": pct, "detail": detail, "done": done,
                   "error": error, "pid": os.getpid(), "ts": time.time()}, f)
    os.replace(tmp, path)


def _elapsed(seconds):
    """`43s` / `2m14s` — an elapsed time a user can compare against their patience."""
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    return "%dm%02ds" % (minutes, secs) if minutes else "%ds" % secs


def _build(venvs_path, requirements, python_executable):
    """Upstream's builder, in one place (and imported at call time, not at module
    import, so a missing `fused` surfaces as a progress error rather than an
    unexplained non-zero exit)."""
    from fused.agent_core.backends.local.venvs import ensure_requirements_venv

    return ensure_requirements_venv(venvs_path, list(requirements), python_executable)


def install(key, progress_dir, venvs_path, requirements, python_executable=None):
    os.makedirs(progress_dir, exist_ok=True)
    summary = ", ".join(requirements)

    # Every record goes through this, and a terminal one LATCHES the file shut.
    #
    # Not politeness — the ordering is the feature. The heartbeat thread and the
    # terminal write both `os.replace` onto `progress.json`, and a beat landing
    # afterwards puts `done: false` back on the wire: the client polls a finished
    # install forever, which is the very "stuck" symptom the heartbeat exists to
    # cure, made permanent. The Event + `join` below normally stop the beat before
    # the terminal write, but `join` has a timeout and a beat can be parked inside
    # its own `_write` on a slow filesystem — so the guarantee lives here, in a lock
    # and a latch, rather than in the hope that the join won the race. (Same
    # reasoning as D181: an ordering that matters is enforced, not asserted.)
    write_lock = threading.Lock()
    finished = []

    def write(stage, pct, detail="", done=False, error=None):
        with write_lock:
            if finished:
                return  # a terminal record is already on disk; nothing may follow it
            _write(progress_dir, stage, pct, detail, done, error)
            # Latched only once the record is actually ON DISK. Latching before the
            # write would make a FAILED terminal write shut the file anyway, and the
            # `except` path's error record — the one carrying the reason — would
            # silently no-op, leaving `done: false` on the wire forever: the same
            # stuck poll this whole mechanism exists to prevent, reached by the
            # opposite route. The lock is what keeps this safe: `_write` and the
            # latch are one atomic step, so no beat can slip between them.
            if done:
                finished.append(True)

    try:
        # `create` and `install` are reported as one call because that is the
        # truth: ensure_requirements_venv does both behind capture_output=True,
        # so the transition between them is not observable from out here. The
        # two stages exist so the UI can say "preparing" before the long wait,
        # not to imply progress inside it.
        write("create", _CREATE_PCT, f"preparing an environment for {summary}")
        detail = f"downloading and installing {len(requirements)} package(s): {summary}"
        write("install", _INSTALL_PCT, detail)

        # The heartbeat. `pct` STAYS at _INSTALL_PCT for the whole download, and the
        # stage stays `install`: there is no computable percentage in here, since
        # upstream captures uv's output, and a bar creeping upward on invented
        # numbers is worse than an honest one that does not move — the number is the
        # thing a waiting user trusts most. What it refreshes is the elapsed time and
        # `ts`, which is the only evidence of liveness that reaches the wire. The
        # client renders the stage as an indeterminate bar (runtime.js), so "alive
        # but unquantified" is expressible without lying.
        stop = threading.Event()
        started = time.time()

        def heartbeat():
            # `Event.wait`, never `sleep`: setting the event wakes it at once, so
            # shutdown costs microseconds instead of up to a full interval — and a
            # beat that fires during teardown is exactly what the latch above exists
            # to absorb.
            while not stop.wait(_HEARTBEAT_S):
                write("install", _INSTALL_PCT,
                      "%s (%s)" % (detail, _elapsed(time.time() - started)))

        # Daemon: a heartbeat wedged in a write must never keep this process alive
        # after its record says done, or `_pid_alive` reads the installer as still
        # running and the page polls a corpse.
        beat = threading.Thread(target=heartbeat, name="env-install-heartbeat",
                                daemon=True)
        beat.start()
        try:
            # `python_executable` is the server's own `_python_executable()`, handed
            # over rather than re-decided: the venv key folds it in, so a value that
            # differs from the server's builds a directory no run ever reads.
            venv_python = _build(venvs_path, requirements, python_executable)
        finally:
            # In a `finally`, so the failure path stops the beat too — and it runs as
            # the exception propagates, i.e. BEFORE the `except` below writes the
            # error record. Both terminal writes are therefore behind the join.
            stop.set()
            beat.join(_HEARTBEAT_JOIN_S)
        write("done", 100, f"installed into {os.path.dirname(os.path.dirname(venv_python))}",
              done=True)
    except BaseException as e:  # noqa: BLE001
        # Verbatim: upstream's message already carries uv's/pip's stderr, which
        # names the real problem (a platform with no wheel, a bad pin, no
        # network). Only the exception class is prefixed, so the page can tell a
        # resolver failure from a disk-quota RuntimeError.
        write("error", 100, "", done=True, error=f"{type(e).__name__}: {e}")
        raise


def main(args):
    """`<key> <progress_dir> <venvs_path> <python_executable> <req>...`

    The empty string in the interpreter slot means None (argv cannot carry it):
    translated here and nowhere else, so `install` receives the real value.
    """
    if len(args) < 5:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    key, progress_dir, venvs_path, python_executable = args[:4]
    install(key, progress_dir, venvs_path, args[4:], python_executable or None)


if __name__ == "__main__":
    main(sys.argv[1:])
