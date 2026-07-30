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
import time

# Stages and their percentages, kept in step with fused_render/envinstall.py's
# STAGES/STAGE_PCT. Duplicated rather than imported because this file must stay
# importable-free of the package (it is spawned as a plain script, and
# `import fused_render` in a detached child is exactly the bootstrap that broke
# once already — see D152).
_CREATE_PCT = 10
_INSTALL_PCT = 25


def _write(progress_dir, stage, pct, detail="", done=False, error=None):
    # Unique temp name, not a shared `progress.json.tmp`: the server writes this
    # same file (envinstall._write) and two writers racing on one temp means the
    # first os.replace consumes the second's file, whose replace then fails.
    path = os.path.join(progress_dir, "progress.json")
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"stage": stage, "pct": pct, "detail": detail, "done": done,
                   "error": error, "pid": os.getpid(), "ts": time.time()}, f)
    os.replace(tmp, path)


def _build(venvs_path, requirements, python_executable):
    """Upstream's builder, in one place (and imported at call time, not at module
    import, so a missing `fused` surfaces as a progress error rather than an
    unexplained non-zero exit)."""
    from fused.agent_core.backends.local.venvs import ensure_requirements_venv

    return ensure_requirements_venv(venvs_path, list(requirements), python_executable)


def install(key, progress_dir, venvs_path, requirements, python_executable=None):
    os.makedirs(progress_dir, exist_ok=True)
    summary = ", ".join(requirements)
    try:
        # `create` and `install` are reported as one call because that is the
        # truth: ensure_requirements_venv does both behind capture_output=True,
        # so the transition between them is not observable from out here. The
        # two stages exist so the UI can say "preparing" before the long wait,
        # not to imply progress inside it.
        _write(progress_dir, "create", _CREATE_PCT, f"preparing an environment for {summary}")
        _write(progress_dir, "install", _INSTALL_PCT,
               f"downloading and installing {len(requirements)} package(s): {summary}")
        # `python_executable` is the server's own `_python_executable()`, handed
        # over rather than re-decided: the venv key folds it in, so a value that
        # differs from the server's builds a directory no run ever reads.
        venv_python = _build(venvs_path, requirements, python_executable)
        _write(progress_dir, "done", 100, f"installed into {os.path.dirname(os.path.dirname(venv_python))}",
               done=True)
    except BaseException as e:  # noqa: BLE001
        # Verbatim: upstream's message already carries uv's/pip's stderr, which
        # names the real problem (a platform with no wheel, a bad pin, no
        # network). Only the exception class is prefixed, so the page can tell a
        # resolver failure from a disk-quota RuntimeError.
        _write(progress_dir, "error", 100, "", done=True, error=f"{type(e).__name__}: {e}")
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
