"""Run the `fused` CLI under this interpreter.

The fusedcli seam's ONE autodetected CLI (fusedcli.fused_cli(), used by
canvases.py): when the `fused` package is importable in the interpreter
running the fused-render server, it spawns ``[sys.executable, <this file>,
*args]`` instead of requiring a console script on disk. That is what makes
the packaged macOS app work — py2app bundles no console scripts and no pip,
but it ships a real, re-invokable interpreter (the executor's ``_child.py``
spawn pattern) with the fused package baked in by build_dmg.sh. Any OTHER
fused install is used only when the user explicitly points
FUSED_RENDER_FUSED_BIN at it — there is no PATH scanning or
well-known-location guessing.

Behaviorally identical to the ``fused`` console script: click reads
``sys.argv[1:]``, and argv[0] is renamed so usage/error text says ``fused``,
not this file's path — with ONE exception, and it is the reason this file is
where the exception lives.

A `fused workbench canvas push` whose target is a canvas clone is routed to
fused-render's own sync manager instead of being dispatched to the CLI (see
``_canvas_push``): the raw push skips the pre-push merge guard and can
unrecoverably destroy a concurrent workbench edit, and it moves the remote
behind the watcher's back. Because this shim is the path every shipping install
takes — the packaged app bakes the pre-release fused into its own interpreter
and has no console scripts — intercepting here covers every push a Claude
session can make, with a real argv to match against. Anything not recognised
with certainty falls through unchanged, and so does any failure to reach the
server.
"""
import sys

if __name__ == "__main__":
    sys.argv[0] = "fused"
    code = None
    try:
        from fused_render._canvas_push import maybe_intercept

        code = maybe_intercept(sys.argv[1:], sys.stdout, sys.stderr)
    except Exception:  # noqa: BLE001
        # Never let the interception break the CLI: whatever went wrong here,
        # dispatching to the real `fused` is the behaviour this file promises.
        code = None
    if code is not None:
        sys.exit(code)

    from fused._cli import main

    main()
