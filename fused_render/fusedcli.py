"""The fused CLI seam shared by deploy.py and account.py.

Everything here answers one of two questions both feature routers ask:
"which fused CLI do I run, and how?" (resolution + child-env hygiene +
error mapping) and "what does the fused CLI's own on-disk state say?"
(login signal, env store reads). It owns NO endpoints and NO subprocess
orchestration of its own — deploy.py runs `fused share …`, account.py runs
`fused cloud …`; both build their child processes from these primitives.

Split out of deploy.py when the account surface landed (see
docs/PLAN-fused-account.md): the two routers must stay mutually acyclic,
and neither may import server.py (server includes both routers).
"""
from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import os
import sys

from fused_render.shell import storage

# Backends that can answer a served URL (flow's HOSTED_BACKENDS): the managed
# Fused control plane, or an AWS env whose serving plane `fused infra serve`
# provisioned. `local` has no serving plane and is never eligible.
HOSTED_BACKENDS = ("fused", "aws")


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
         [fused] extra (including via POST /api/deploy/install) AND the
         packaged macOS app, whose py2app bundle has no console scripts but
         bakes the fused package in (build_dmg.sh) and whose sys.executable
         is a real re-invokable interpreter (the executor's _child.py spawn
         pattern).
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
    override channel) — deploy runs are env-targeted. Account runs
    (`cloud login/logout/orgs/setup`, `env default/delete`) pass None and get
    the variable CLEARED instead of inherited: today's `fused cloud` commands
    never read it, but an ambient value in the server's own environment
    (common when testing deploys) must not leak an env target into a child
    whose scope is the account.
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
    that runs the same baked-in CLI the Deploy button uses, at
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


def credentials_path() -> str:
    """Where the fused CLI keeps its control-plane credentials
    (`fused cloud login` → ~/.openfused/fused-cloud-credentials.json), with
    the same env override the CLI itself honors (onboarding.py)."""
    return os.environ.get("OPENFUSED_FUSED_CLOUD_CREDENTIALS") or os.path.expanduser(
        "~/.openfused/fused-cloud-credentials.json"
    )


def fused_cloud_logged_in() -> bool:
    """Whether the fused CLI's control-plane credentials exist on disk.

    Presence-only, deliberately: an expired-but-refreshable token still works
    (the CLI refreshes silently), and validating deeper would duplicate the
    CLI's own logic — this signal exists so the UI can warn BEFORE a doomed
    action (deploy to a managed env with no login at all) and so the account
    page can render its signed-in/out state cheaply; the CLI stays the
    authority at action time. The account page's deeper probe
    (`fused cloud orgs`) is the authoritative check when one is wanted.
    """
    return os.path.isfile(credentials_path())


def envs_file() -> str:
    # The same override the fused CLI itself honors (environments.py), so a
    # relocated store stays consistent between the CLI and this reader.
    return os.environ.get("OPENFUSED_ENVS_FILE") or os.path.expanduser("~/.openfused/envs.json")


def all_envs() -> dict:
    """Every env in the store (any backend) + the store's own default pointer.

    The account page's management view: unlike eligible_envs (the deploy
    picker, hosted-only, with a deploy-oriented default derivation) this is
    the raw store — local envs included, each flagged with whether it can be
    a deploy target, and `default` exactly as the store records it.
    """
    data = storage.read_json(envs_file())
    raw_envs = data.get("envs") if isinstance(data, dict) else None
    envs = []
    if isinstance(raw_envs, dict):
        for entry in raw_envs.values():
            if not isinstance(entry, dict):
                continue
            name, backend = entry.get("name"), entry.get("backend")
            if isinstance(name, str) and isinstance(backend, str):
                envs.append(
                    {"name": name, "backend": backend, "hosted": backend in HOSTED_BACKENDS}
                )
    envs.sort(key=lambda e: e["name"])
    default = data.get("default") if isinstance(data, dict) else None
    return {"envs": envs, "default": default if isinstance(default, str) else None}


def eligible_envs() -> dict:
    """Hosted envs from the fused store + the picker's default.

    The deploy-picker view over one all_envs() read (a direct store read,
    like the flow app's readEnvs, so the picker renders even when the CLI is
    not installed yet). Default pick: OPENFUSED_ENV when it names an eligible
    env (explicit intent for this process), else the first `fused`-backend
    env — preferring the store's own default when that is one — else the
    store default, else the first eligible.
    """
    store = all_envs()
    envs = [{"name": e["name"], "backend": e["backend"]} for e in store["envs"] if e["hosted"]]

    by_name = {e["name"]: e for e in envs}
    store_default = store["default"]
    fused_backed = [e["name"] for e in envs if e["backend"] == "fused"]

    default = None
    ambient = os.environ.get("OPENFUSED_ENV")
    if ambient in by_name:
        default = ambient
    elif store_default in by_name and by_name[store_default]["backend"] == "fused":
        default = store_default
    elif fused_backed:
        default = fused_backed[0]
    elif store_default in by_name:
        default = store_default
    elif envs:
        default = envs[0]["name"]

    return {"envs": envs, "default_env": default, "envs_file": envs_file()}
