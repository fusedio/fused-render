"""The fused CLI seam: which `fused` CLI to run, and how.

Answers "which fused CLI do I run, and how?" (resolution + child-env hygiene +
error mapping) for canvases.py, which runs `fused login`/`canvas push`/`canvas
pull` through it. It owns NO endpoints and NO subprocess orchestration of its
own — canvases.py builds its child processes from these primitives.

Originally split out of deploy.py (now removed) when the account surface
landed (SPEC §27, DECISIONS D112), to keep feature routers mutually acyclic;
canvases.py is the sole remaining consumer.
"""
from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import os
import sys


@dataclasses.dataclass(frozen=True)
class FusedCli:
    """The resolved fused CLI: the command vector, and whether it is an
    EXTERNAL interpreter (a FUSED_RENDER_FUSED_BIN override) — external
    children get PYTHONHOME/PYTHONPATH scrubbed so the packaged app's
    bundle-scoped interpreter env can't poison them (see child_env)."""

    command: list[str]
    external: bool


def fused_cli() -> FusedCli | None:
    """Resolve the fused CLI, or None when there is none.

    Exactly TWO sources — one explicit, one autodetected — and nothing else
    (no venv-bin scan, no PATH lookup, no well-known-location guessing; a CLI
    this server didn't get from its own interpreter runs only because the
    user explicitly configured it):

      1. FUSED_RENDER_FUSED_BIN — trusted verbatim, split on whitespace so a
         compound command works (e.g. "uv run fused"). Mirrors the flow app's
         OPENFUSED_BIN seam; also how tests substitute a stub CLI.
      2. the `fused` package importable in THIS interpreter — run as
         ``[sys.executable, _fused_cli.py]`` (the shim sets argv[0] and calls
         fused._cli.main). Covers a venv server that pip-installed the
         [fused] extra AND the packaged macOS app, whose py2app bundle has no
         console scripts but bakes the fused package in (build_dmg.sh) and
         whose sys.executable is a real re-invokable interpreter (the
         executor's _child.py spawn pattern).
    """
    override = os.environ.get("FUSED_RENDER_FUSED_BIN")
    if override:
        parts = override.split()
        return FusedCli(command=parts, external=True) if parts else None
    try:
        importable = importlib.util.find_spec("fused") is not None
    except (ImportError, ValueError):
        importable = False
    if importable:
        shim = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fused_cli.py")
        return FusedCli(command=[sys.executable, shim], external=False)
    return None


def child_env(cli: FusedCli, env_name: str | None = None) -> dict[str, str]:
    """The child environment for a fused CLI run.

    OPENFUSED_ENV targets the chosen env when one is given (the CLI's own
    override channel). Callers that have no env to target (canvases.py's
    `fused login`/`canvas push`/`canvas pull`) pass None and get the variable
    CLEARED instead of inherited, so an ambient value in the server's own
    environment can't leak an unrelated env target into the child.
    For an EXTERNAL cli (FUSED_RENDER_FUSED_BIN), interpreter-scoped vars are
    scrubbed: inside the packaged macOS app the process carries PYTHONHOME/
    PYTHONPATH pointing into the bundle, which would break any other Python's
    interpreter (same scrub the las template does for its external spawns).
    The in-interpreter shim keeps them — they are what make sys.executable
    work in the bundle.
    """
    child = dict(os.environ)
    if env_name is not None:
        child["OPENFUSED_ENV"] = env_name
    else:
        child.pop("OPENFUSED_ENV", None)
    if cli.external:
        for var in ("PYTHONHOME", "PYTHONPATH"):
            child.pop(var, None)
    return child


def setup_cli_hint() -> str:
    """The command users type in a terminal for one-time CLI setup
    (`fused env create`, `fused cloud setup`, `fused cloud login`).

    Inside the packaged macOS app (py2app sets sys.frozen) there is no
    user-facing `fused` on PATH — but the bundle ships a terminal wrapper
    that runs the same baked-in CLI canvases.py uses, at
    ``Contents/Resources/bin/fused`` (build_dmg.sh §4c — under Resources, not
    MacOS, because a shell script in a code directory breaks the codesign
    bundle seal). sys.executable is ``…/Contents/MacOS/python``; the wrapper
    is resolved relative to it. Point guidance at it so a .app user never
    needs a separate fused install.
    """
    if getattr(sys, "frozen", None) == "macosx_app":
        contents = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
        wrapper = os.path.join(contents, "Resources", "bin", "fused")
        if os.path.isfile(wrapper):
            return wrapper
    return "fused"


def cli_error(stderr: str, fallback: str) -> str:
    """Last non-empty stderr line with click's `Error: ` prefix stripped — the
    CLI's messages already name the fix, so they reach the UI verbatim.

    One adjustment: login errors say `fused cloud login`, which doesn't
    resolve inside the packaged app (no `fused` on PATH) — when the bundled
    wrapper is the setup CLI, its real path is appended so the instruction is
    runnable as printed.
    """
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    message = lines[-1] if lines else fallback
    message = message.removeprefix("Error: ")
    setup = setup_cli_hint()
    if setup != "fused" and "fused cloud login" in message:
        message += f" (in this app: {setup} cloud login)"
    return message


