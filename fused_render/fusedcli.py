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
import logging
import os
import shlex
import stat
import sys

logger = logging.getLogger(__name__)


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


def _installed_fused_version() -> str | None:
    """The version of the `fused` DISTRIBUTION installed in this interpreter.

    Distribution metadata, deliberately NOT ``fused.__version__``: a dev venv
    once held an older fused than pyproject pinned and every ``/api/run`` died
    with SIGSEGV, while that attribute reported misleadingly — so the attribute
    is not evidence of which wheel is actually installed.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("fused")
    except PackageNotFoundError:
        return None
    except Exception:  # noqa: BLE001 — a diagnostic must never raise
        return None


def _pinned_fused_version() -> str | None:
    """What THIS project pins `fused` to, from fused-render's own installed
    metadata (``fused==X`` in Requires-Dist) — the only source that exists at
    runtime, since a shipped app has no pyproject.toml."""
    try:
        from importlib.metadata import PackageNotFoundError, requires

        try:
            reqs = requires("fused-render") or []
        except PackageNotFoundError:
            return None
        for req in reqs:
            text = req.replace(" ", "")
            if text.startswith("fused=="):
                return text[len("fused=="):].split(";")[0].split(",")[0]
    except Exception:  # noqa: BLE001
        return None
    return None


def log_cli_provenance() -> str | None:
    """Log which `fused` CLI is in effect when it is NOT the one this app ships,
    and return the warning text (or None when all is well).

    A DEVELOPER diagnostic, not user-facing. For a shipping user there is no
    other fused: the DMG bakes the pre-release wheel into the app's own
    interpreter and `fused_cli()`'s second branch is the only path they take —
    so both states this reports (an explicit override, or an installed fused
    that does not match the pin) are conditions only a dev checkout reaches.
    That is precisely why they deserve a log rather than UI: the failures they
    cause are baffling and far from their cause. An older fused has no manifest
    shims, and BOTH the manifest-based two-way canvas sync and the clone's
    CLAUDE.md seeding are gated on those — so the override can quietly disable
    the machinery that depends on it, and it structurally bypasses the
    `_fused_cli.py` push interception too.

    The override itself stays fully supported: it is how the test suite
    substitutes a stub CLI, and how a dev points at a local build.
    """
    override = os.environ.get("FUSED_RENDER_FUSED_BIN")
    if override:
        message = (
            "using the fused CLI from FUSED_RENDER_FUSED_BIN (%s) instead of "
            "the one this app ships. The manifest shims the two-way canvas "
            "sync and clone CLAUDE.md seeding depend on may be missing, and "
            "the canvas-push interception is bypassed." % override)
        logger.warning("%s", message)
        return message
    installed = _installed_fused_version()
    if installed is None:
        # No fused importable at all: `fused_cli()` returns None and every
        # canvases endpoint already says so in its own error. Nothing to add.
        return None
    pinned = _pinned_fused_version()
    if pinned and installed != pinned:
        message = (
            "the installed fused is %s but this project pins %s. Version drift "
            "here has produced SIGSEGV on every /api/run before; reinstall "
            "with `pip install -e \".[fused]\"`." % (installed, pinned))
        logger.warning("%s", message)
        return message
    return None


def workbench_env() -> str:
    """The Fused environment the workbench features target — ONE knob
    (FUSED_RENDER_WORKBENCH_ENV, default unstable) shared by canvases.py's
    iframe URL + CLI runs AND the `fused` wrapper handed to Claude sessions
    (export_fused_cli_env), so what Claude pushes lands in the same
    environment the canvases iframe shows. Lives here rather than in
    canvases.py because canvases imports this module, not the other way
    around."""
    return os.environ.get("FUSED_RENDER_WORKBENCH_ENV", "unstable")


# The env var that carries the wrapper dir to the templates (SPEC PY-15):
# set by `export_fused_cli_env` before the server serves, read only by
# `templates/shared/appenv.py:fused_cli_dir`. Absent means "no fused CLI" —
# a template then neither pre-allows `fused` nor mentions it in its prompt.
CLI_DIR_ENV = "FUSED_RENDER_FUSED_CLI_DIR"

# The wrapper dir's basename under home_dir(). A name of its own (not the
# skill plugin's dir) because it is prepended to PATH, and a PATH entry
# should hold executables and nothing else.
CLI_BIN_SUBDIR = "fused-bin"


def _wrapper_text(cli: FusedCli) -> str:
    """The wrapper script for the resolved CLI, per platform.

    It bakes in what canvases.py's `_cli_env`/`child_env` set explicitly for
    its own runs, as DEFAULTS the caller can still override from the command
    line (`FUSED_ENV=prod fused ...` wins — an unconditional export here
    would silently clobber a deliberate target):
      * FUSED_ENV defaults to workbench_env(), so a bare `fused workbench
        canvas push` from a Claude session hits the same environment the
        canvases iframe shows, not the CLI's own default.
      * an EXTERNAL cli gets PYTHONHOME/PYTHONPATH unset (same scrub as
        child_env — the packaged app's bundle-scoped interpreter vars break
        any other Python). The in-interpreter shim keeps them: they are what
        make sys.executable work in the bundle.
    """
    env_name = workbench_env()
    if os.name == "nt":
        lines = ["@echo off",
                 f'if not defined FUSED_ENV set "FUSED_ENV={env_name}"']
        if cli.external:
            lines += ["set PYTHONHOME=", "set PYTHONPATH="]
        quoted = " ".join('"%s"' % part for part in cli.command)
        lines.append(f"{quoted} %*")
        return "\r\n".join(lines) + "\r\n"
    lines = ["#!/bin/sh",
             "# generated by fused-render (fusedcli.export_fused_cli_env);",
             "# rewritten on every server start — do not edit.",
             f'[ -n "${{FUSED_ENV:-}}" ] || FUSED_ENV={shlex.quote(env_name)}',
             "export FUSED_ENV"]
    if cli.external:
        lines.append("unset PYTHONHOME PYTHONPATH")
    quoted = " ".join(shlex.quote(part) for part in cli.command)
    lines.append(f'exec {quoted} "$@"')
    return "\n".join(lines) + "\n"


def export_fused_cli_env() -> str | None:
    """Write a `fused` wrapper the sessions we spawn can run, put its dir on
    PATH, and publish it for the templates; returns the wrapper dir, or None
    when there is no CLI to wrap.

    Called from `server.export_app_env`, i.e. once before the server serves,
    so every child inherits both the PATH entry and CLI_DIR_ENV — same
    mechanism as `_export_bundled_uv_path` (PATH) and the skill plugin
    (env contract, D216). Best-effort and never raises: a chat without the
    CLI is still a working chat.

    The wrapper is regenerated on every call (a few lines — cheaper to
    rewrite than to fingerprint) so a changed FUSED_RENDER_FUSED_BIN or
    workbench env takes effect on restart.
    """
    from fused_render.shell.storage import home_dir

    # Say so in the log when the CLI in effect is not the one this app ships.
    # Here because this runs once before serving, and a dev whose canvas sync
    # silently lost its manifest shims has no other breadcrumb.
    log_cli_provenance()
    cli = fused_cli()
    if cli is None:
        os.environ.pop(CLI_DIR_ENV, None)
        return None
    bin_dir = os.path.join(home_dir(), CLI_BIN_SUBDIR)
    wrapper = os.path.join(bin_dir, "fused.cmd" if os.name == "nt" else "fused")
    try:
        os.makedirs(bin_dir, exist_ok=True)
        with open(wrapper, "w", encoding="utf-8", newline="") as fh:
            fh.write(_wrapper_text(cli))
        if os.name != "nt":
            os.chmod(wrapper, os.stat(wrapper).st_mode
                     | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError as exc:
        logger.warning("could not write the fused CLI wrapper at %s: %s",
                       wrapper, exc)
        os.environ.pop(CLI_DIR_ENV, None)
        return None
    path = os.environ.get("PATH", "")
    if bin_dir not in path.split(os.pathsep):
        os.environ["PATH"] = (bin_dir + os.pathsep + path) if path else bin_dir
    os.environ[CLI_DIR_ENV] = bin_dir
    return bin_dir


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


