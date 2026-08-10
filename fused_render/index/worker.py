"""Detached scan worker entrypoint: `python -m fused_render.index.worker <run_dir>`.

A module rather than a script path (OpenIndex spawned `Popen([python,
__file__, "--worker", run_dir])`) because a py2app bundle has no source file to
point at — but it does have the package on `sys.path`.

Everything this process needs is in `<run_dir>/spec.json`, written by
`runner.start`: the root, the full/incremental flag, and the IndexConfig. It
re-derives nothing from the environment, so a home that moved between the
request and the spawn cannot make the worker compact into a directory nobody
reads.
"""
import os
import sys

from fused_render.index.scan import run_scan


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m fused_render.index.worker <run_dir>",
              file=sys.stderr)
        return 2
    # Detach from the server's session HERE, in the child, not via
    # `start_new_session=True` at the spawn: that kwarg forces CPython off
    # posix_spawn onto fork()+exec, and a fork of a server that has touched
    # pyproj/rasterio runs PROJ's atfork handler and dies with SIGSEGV before
    # this line is ever reached (see envinstall.py, same discipline). setsid
    # in the child after exec is the safe equivalent — the scan must outlive
    # the request and ignore the server's terminal signals.
    if hasattr(os, "setsid"):
        try:
            os.setsid()
        except OSError:
            pass  # already a session leader (spawned from a shell)
    run_scan(argv[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
