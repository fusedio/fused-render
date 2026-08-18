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
not this file's path.
"""
import sys

if __name__ == "__main__":
    sys.argv[0] = "fused"
    from fused._cli import main

    main()
