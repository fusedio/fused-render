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
import re
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


def _split_override(override: str) -> list[str]:
    """FUSED_RENDER_FUSED_BIN as an argv list, honouring quotes.

    Splitting is the point — a compound command ("uv run fused") is documented
    and supported. But a PLAIN whitespace split cannot express a path containing
    a space: it comes apart, so the override either "wasn't found" or ran
    something else entirely, with nothing in the failure pointing at the space
    as the cause. Quoting that component (or backslash-escaping the space, e.g.
    `/opt/my\\ app/py`) works, because this is the POSIX shell lexer.

    An unbalanced quote makes shlex raise. That is a malformed env var, not a
    reason to take down every caller (fused_cli() is on the canvases request
    path), so it degrades to the historical whitespace split and says so.

    Still not expressible: an UNQUOTED path containing a
    space. That is ambiguous by construction — a shell cannot read it either —
    and it is why the quoting above is the fix rather than some heuristic that
    tries to guess where the path ends.
    """
    try:
        return shlex.split(override)
    except ValueError:
        logger.warning(
            "FUSED_RENDER_FUSED_BIN is not parseable as a command line (%r) — "
            "unbalanced quote? Falling back to a plain whitespace split, which "
            "cannot handle a path containing spaces.", override)
        return override.split()


def fused_cli() -> FusedCli | None:
    """Resolve the fused CLI, or None when there is none.

    Exactly TWO sources — one explicit, one autodetected — and nothing else
    (no venv-bin scan, no PATH lookup, no well-known-location guessing; a CLI
    this server didn't get from its own interpreter runs only because the
    user explicitly configured it):

      1. FUSED_RENDER_FUSED_BIN — trusted verbatim, split into an argv so a
         compound command works (e.g. "uv run fused"); quote a component that
         contains spaces (see _split_override). Mirrors the flow app's
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
        parts = _split_override(override)
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


_PINNED_FUSED_RE = re.compile(r"^fused(?:\[[^\]]*\])?==")


def _pinned_fused_version() -> str | None:
    """What THIS project pins `fused` to, from fused-render's own installed
    metadata (``fused==X`` or ``fused[extra1,extra2]==X`` in Requires-Dist) —
    the only source that exists at runtime, since a shipped app has no
    pyproject.toml.

    The optional `[...]` extras group must be matched and stripped, not just
    the bare name: `[bundled]`/`[fused]` pin `fused[aws,mcp]==...` (since fused
    2.9.3b9/b10, boto3, pyarrow, `pyjwt[crypto]` and mcp sit behind those extras,
    so a bare `fused==` pin no longer builds a working DMG venv or a startable
    `fused app serve`; the pyproject.toml comment on that line has the detail). A plain `startswith("fused==")`
    check would silently stop matching the moment extras were added, turning
    this drift check into a permanent no-op with no failing test — exactly the
    class of defect `test_the_pin_is_discoverable_at_runtime` exists to catch.
    """
    try:
        from importlib.metadata import PackageNotFoundError, requires

        try:
            reqs = requires("fused-render") or []
        except PackageNotFoundError:
            return None
        for req in reqs:
            text = req.replace(" ", "")
            m = _PINNED_FUSED_RE.match(text)
            if m:
                return text[m.end():].split(";")[0].split(",")[0]
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


# The env var that pins the workbench environment for the whole process. When
# set it wins over the preference and the Preferences row shows as locked.
WORKBENCH_ENV_VAR = "FUSED_RENDER_WORKBENCH_ENV"
# The environments a canvas can belong to (the `<env>` segment of
# `~/Fused/canvas/<env>/<name>`, every canvases endpoint's `env`) and that the
# DEFAULT preference may name. The env var still accepts the CLI's full table
# (stg, dev, a PR number) — but canvases.py refuses every canvas call under a
# pin outside this tuple (`_require_env`).
WORKBENCH_ENV_CHOICES = ("unstable", "prod", "custom")
# The choice that names no Fused of its own: its runs get FUSED_ENV UNSET, so
# the fused SDK keeps the `base_url` / `shared_udf_base_url` it loaded from
# `~/.fused/settings.toml` — `fused._env("prod")` would overwrite them with
# the hosted defaults (owner, 2026-10-21). Use `set_fused_env`, never
# `child["FUSED_ENV"] = env`, wherever a run is pointed at an environment.
CUSTOM_WORKBENCH_ENV = "custom"
# Owner, 2026-10-17: unstable while embed v3 landed on the unstable workbench
# first; owner, 2026-10-22: prod now that it is live there. Only the default
# moves — a stored `workbench_env` preference and every clone's own env are
# untouched.
DEFAULT_WORKBENCH_ENV = "prod"


def workbench_env() -> str:
    """The DEFAULT Fused environment — not "the" environment any more: a
    canvas carries its own (the `<env>` of `~/Fused/canvas/<env>/<name>`), and
    a prod canvas and an unstable one sync at once (owner, 2026-10-20). What
    still follows this default: the canvas catalog's initial environment, the
    `fused` wrapper's FUSED_ENV when a session runs it OUTSIDE a clone (inside
    one the wrapper takes the clone's env from the path —
    `_wrapper_text`), and file / app SHARING uploads (share_file.py,
    share_app.py).

    Resolved at CALL time, never cached by a caller: the env var
    (FUSED_RENDER_WORKBENCH_ENV, a process-level pin that also restricts every
    canvases call to this env) wins; else the `workbench_env` preference in
    prefs.json (PUT /api/prefs, which rewrites the wrapper); else
    DEFAULT_WORKBENCH_ENV. Lives here rather than in canvases.py because
    canvases imports this module, not the other way around."""
    pinned = os.environ.get(WORKBENCH_ENV_VAR)
    if pinned:
        return pinned
    from fused_render.shell import prefs

    stored = prefs.read_prefs().get("workbench_env")
    if stored in WORKBENCH_ENV_CHOICES:
        return stored
    return DEFAULT_WORKBENCH_ENV


def set_fused_env(child: dict[str, str], env: str) -> None:
    """Point the child environment `child` at `env`: FUSED_ENV=<env>, or
    FUSED_ENV REMOVED for CUSTOM_WORKBENCH_ENV — removed rather than left as
    inherited, so an ambient value in the server's own environment cannot
    retarget a run meant to follow `~/.fused/settings.toml`."""
    if env == CUSTOM_WORKBENCH_ENV:
        child.pop("FUSED_ENV", None)
    else:
        child["FUSED_ENV"] = env


def workbench_env_locked() -> bool:
    """Whether the env var pins the environment (the preference is ignored
    and the Preferences row says so)."""
    return bool(os.environ.get(WORKBENCH_ENV_VAR))


# The env var that carries the wrapper dir to the templates (SPEC PY-15):
# set by `export_fused_cli_env` before the server serves, read only by
# `templates/shared/appenv.py:fused_cli_dir`. Absent means "no fused CLI" —
# a template then neither pre-allows `fused` nor mentions it in its prompt.
CLI_DIR_ENV = "FUSED_RENDER_FUSED_CLI_DIR"

# The wrapper dir's basename under home_dir(). A name of its own (not the
# skill plugin's dir) because it is prepended to PATH, and a PATH entry
# should hold executables and nothing else.
CLI_BIN_SUBDIR = "fused-bin"


# The env var that carries the `fused-render` wrapper's absolute path to the
# templates: set by `export_render_wrapper` before the server serves, read only
# by `templates/shared/appenv.py:render_bin`. Absent means "no wrapper", and the
# MCP panel then does not offer registration.
RENDER_BIN_ENV = "FUSED_RENDER_BIN"

# The wrapper's filename inside the fused-bin dir: what an MCP entry runs as
# `fused-render app serve <dir>`.
RENDER_WRAPPER_NAME = "fused-render"


def _wrapper_text(cli: FusedCli) -> str:
    """The wrapper script for the resolved CLI, per platform.

    It bakes in what canvases.py's `_cli_env`/`child_env` set explicitly for
    its own runs, as DEFAULTS the caller can still override from the command
    line (`FUSED_ENV=prod fused ...` wins — an unconditional export here
    would silently clobber a deliberate target):
      * FUSED_ENV, when unset, comes from the working directory first: under
        `~/Fused/canvas/<env>/` it is that `<env>`, because the path segment
        IS the clone's environment (D1382) and a session in a prod clone must
        reach prod even when the default is another env — a bare `fused
        workbench canvas pull`/`validate`/`whoami` there would otherwise hit
        the wrong Fused. Both `$PWD` and `pwd -P` are matched against both
        the base as given and its realpath, so a symlinked path (macOS
        `/var` → `/private/var`, a symlinked home) still matches. Outside a
        clone it defaults to workbench_env(), the DEFAULT environment.
        CUSTOM_WORKBENCH_ENV is the exception at both steps: a custom clone,
        or a custom default, leaves FUSED_ENV unset (and unexported), so the
        CLI keeps the endpoint `~/.fused/settings.toml` names.
      * an EXTERNAL cli gets PYTHONHOME/PYTHONPATH unset (same scrub as
        child_env — the packaged app's bundle-scoped interpreter vars break
        any other Python). The in-interpreter shim keeps them: they are what
        make sys.executable work in the bundle.
    """
    env_name = workbench_env()
    lines = ["#!/bin/sh",
             "# generated by fused-render (fusedcli.export_fused_cli_env);",
             "# rewritten on every server start — do not edit.",
             *_clone_env_lines()]
    if env_name != CUSTOM_WORKBENCH_ENV:
        lines.append('[ -n "${FUSED_ENV:-}" ] || [ -n "${_fused_custom:-}" ] || '
                     f"FUSED_ENV={shlex.quote(env_name)}")
    # Exported only when set: an unset FUSED_ENV is how a custom run keeps
    # the settings.toml endpoint.
    lines.append('[ -z "${FUSED_ENV:-}" ] || export FUSED_ENV')
    if cli.external:
        lines.append("unset PYTHONHOME PYTHONPATH")
    quoted = " ".join(shlex.quote(part) for part in cli.command)
    lines.append(f'exec {quoted} "$@"')
    return "\n".join(lines) + "\n"


def _clone_env_lines() -> list[str]:
    """The wrapper's cwd rule: FUSED_ENV from a canvas clone's path segment
    when unset (see `_wrapper_text`) — or, for a custom clone, the
    `_fused_custom` marker that stops the default from filling it in, since
    unset IS that clone's target. canvases is imported lazily: it imports
    this module at its top. Best-effort like the whole wrapper: without the
    rule a session in a clone falls back to the default env, which the push
    interception still refuses for a clone of the other env."""
    try:
        from fused_render.canvases import canvases_base

        raw = os.path.abspath(canvases_base())
    except Exception:  # noqa: BLE001 — never take the wrapper down
        logger.warning("could not resolve the canvases folder for the fused "
                       "wrapper's clone rule", exc_info=True)
        return []
    bases = list(dict.fromkeys((os.path.realpath(raw), raw)))
    arms = [f"      {shlex.quote(base)}/{env}/*) "
            + ("_fused_custom=1" if env == CUSTOM_WORKBENCH_ENV else f"FUSED_ENV={env}")
            + "; break ;;"
            for base in bases for env in WORKBENCH_ENV_CHOICES]
    return ['if [ -z "${FUSED_ENV:-}" ]; then',
            '  for _fused_cwd in "$PWD/" "$(pwd -P 2>/dev/null)/"; do',
            '    case "$_fused_cwd" in',
            *arms,
            "    esac",
            "  done",
            "fi"]


def _write_executable(path: str, text: str) -> None:
    """Write `text` to `path` as an executable script, creating its dir.

    Written beside and renamed over: the wrappers are also REWRITTEN while the
    server runs (the default environment changed, PUT /api/prefs), and a Claude
    session exec'ing one in that instant must see the old file or the new one,
    never a truncated one.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.chmod(tmp, os.stat(tmp).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(tmp, path)


def render_wrapper_text() -> str:
    """The `fused-render` wrapper: this interpreter running `fused_render.cli`.

    No console script exists in the packaged builds, so the wrapper names the
    interpreter and module outright, the way the supervisor starts the server.
    The bundle-scoped PYTHONHOME/PYTHONPATH are baked in when this process has
    them: an MCP host starts the entry with none of our environment, and in the
    bundle they are what make the interpreter run.
    """
    lines = ["#!/bin/sh",
             "# generated by fused-render (fusedcli.export_render_wrapper);",
             "# rewritten on every server start — do not edit."]
    for var in ("PYTHONHOME", "PYTHONPATH"):
        value = os.environ.get(var)
        if value:
            lines.append(f"export {var}={shlex.quote(value)}")
    lines.append(f'exec {shlex.quote(sys.executable)} -m fused_render.cli "$@"')
    return "\n".join(lines) + "\n"


def export_render_wrapper() -> str | None:
    """Write the `fused-render` wrapper under home_dir()/fused-bin and publish its
    path in RENDER_BIN_ENV; returns the path, or None when it cannot be written.

    Independent of the `fused` CLI: `fused-render app serve` runs on this
    interpreter alone, so the wrapper exists on a machine without the `[fused]`
    extra. Regenerated on every call, once before the server serves. Never
    raises.
    """
    from fused_render.shell.storage import home_dir

    wrapper = os.path.join(home_dir(), CLI_BIN_SUBDIR, RENDER_WRAPPER_NAME)
    try:
        _write_executable(wrapper, render_wrapper_text())
    except OSError as exc:
        logger.warning("could not write the fused-render wrapper at %s: %s", wrapper, exc)
        os.environ.pop(RENDER_BIN_ENV, None)
        return None
    os.environ[RENDER_BIN_ENV] = wrapper
    return wrapper


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
    rewrite than to fingerprint) so a changed FUSED_RENDER_FUSED_BIN or script
    interpreter takes effect on restart; PUT /api/prefs calls it again when
    the default environment changes.
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
    wrapper = os.path.join(bin_dir, "fused")
    try:
        _write_executable(wrapper, _wrapper_text(cli))
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


