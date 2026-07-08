"""Deploy a renderable page to a hosted environment through the `fused` CLI.

Export (export.py) stops at a local bundle directory; this module is the next
step: it turns "I have a page" into "I have a URL", by orchestrating the
already-implemented `fused share` CLI group (the fused repo,
spec/serve/share-links.md + spec/serve/fused-render.md). fused-render itself
still hosts nothing and mints nothing — every mutation is a `fused share …`
child process, exactly the pattern the flow app uses for project deploys
(flow repo, spec/app/deploy-project.md). What this module owns:

  * **The fused CLI seam.** Deploying needs the `fused` package, which may not
    be installed in the venv running this server. `fused_cli()` resolves it
    from exactly two sources: an explicit FUSED_RENDER_FUSED_BIN override, or
    — the ONE autodetected source — the `fused` package importable in this
    interpreter, run via the `_fused_cli.py` shim under sys.executable (which
    is what the packaged macOS app uses: its bundle bakes the package in and
    has no console scripts). `POST /api/deploy/install` pip-installs the
    wheel pinned by `PINNED_FUSED_REQUIREMENT` into the server's interpreter
    when it is missing (Python 3.11+ with pip).
  * **Export to a temporary bundle.** Each deploy re-exports the page into a
    fresh temp directory (`export.export_page`) and hands that bundle to
    `share create`/`repoint`; the bundle is deleted afterwards — nothing to
    manage on disk.
  * **Env choice.** Eligible deploy targets are the *hosted* environments in
    the fused CLI's own store (`~/.openfused/envs.json`, OPENFUSED_ENVS_FILE
    override): backends `fused` (managed) and `aws` (self-provisioned serving
    plane) — never `local`, which has no serving plane. The default is the
    managed `fused`-backend env when one exists. The chosen env is targeted by
    setting OPENFUSED_ENV on the child, the CLI's own override channel.
  * **The URL, and a thin pointer.** `share create` is the only URL-minting
    operation and returns the URL exactly once (the managed backend; an AWS
    env returns token+path only — the URL field stays null there, matching
    flow's defensive parse). A thin per-page pointer at
    ~/.fused-render/deployments.json (shell/storage) remembers env/token/URL
    so the shell can re-show the link, mark the file as deployed, and make a
    redeploy hit the SAME token (`share repoint` — stable URL). `share list`
    stays the authority: status is reconciled against it when the modal opens,
    and the share list endpoint joins mounts back to local pages via the
    pointer store.

Deploys are **public share links** (opaque, unguessable capability tokens —
`share create --public` with no --token): per spec/serve/fused-render.md,
authed mounts can't serve a hosted page's asset GETs yet, so public is the one
posture that works fully today.

No import of server.py (server imports this router — keep it acyclic); the
X-Fused guard is duplicated locally like shell/bookmarks.py does.
"""
from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.export import ExportError, export_page, plan_export
from fused_render.shell import storage

router = APIRouter()

# Backends that can answer a served URL (flow's HOSTED_BACKENDS): the managed
# Fused control plane, or an AWS env whose serving plane `fused infra serve`
# provisioned. `local` has no serving plane and is never eligible.
HOSTED_BACKENDS = ("fused", "aws")

# create/repoint upload the bundle (inline base64 on the fused backend) — give
# them the same generous budget flow uses; list is a cheap read.
SHARE_TIMEOUT = 120.0
LIST_TIMEOUT = 60.0
# pip resolving + downloading the pinned fused wheel and its dependency tree.
INSTALL_TIMEOUT = 600.0


class DeployError(Exception):
    """A user-correctable deploy failure; its message is returned verbatim as a
    400 {"error"} — including the fused CLI's own error lines, which already
    name the fix (`fused cloud login`, `fused infra serve`, …)."""


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Same D3 guard as server._require_fused (a custom header forces a CORS
    # preflight that fails cross-origin, blocking blind foreign POSTs).
    # Duplicated deliberately: deploy.py must not import server (no cycle —
    # server includes this router).
    if x_fused != "1":
        return JSONResponse({"error": "missing or invalid X-Fused header"}, status_code=403)
    return None


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# -- the fused CLI seam -------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class FusedCli:
    """The resolved fused CLI: the command vector, and whether it is an
    EXTERNAL interpreter (a FUSED_RENDER_FUSED_BIN override) — external
    children get PYTHONHOME/PYTHONPATH scrubbed so the packaged app's
    bundle-scoped interpreter env can't poison them (see _child_env)."""

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


# The fused wheel the one-click install lands (POST /api/deploy/install) —
# the single IN-CODE source of the pin. pyproject.toml's `[fused]` extra must
# point at the SAME wheel; tests/test_deploy.py pins the two together. A code
# constant, deliberately NOT importlib.metadata.requires("fused-render"):
# dist-info metadata is absent on a source-tree run and on app bundles that
# strip dist-info, and it goes STALE on an editable install that predates the
# extra (metadata only refreshes on reinstall) — all of which disabled the
# install button exactly when it mattered. The constant ships in the same
# file as the code that uses it, so it is always as current as the server.
PINNED_FUSED_REQUIREMENT = (
    "fused @ https://fused-magic.s3.us-west-2.amazonaws.com/fused-2.9.2.post5-py3-none-any.whl"
)
# The wheel's own environment marker (python_version >= "3.11"), enforced here
# because pip is handed the marker-free requirement above.
FUSED_MIN_PYTHON = (3, 11)


def _pip_available() -> bool:
    """Whether this interpreter can `python -m pip` at all.

    False for embedded/packaged interpreters (e.g. the DMG app's bundled
    CPython) — there the install button is pointless and the reason string
    routes the user to FUSED_RENDER_FUSED_BIN / an outside install instead.
    """
    import importlib.util

    return importlib.util.find_spec("pip") is not None


def cli_status() -> dict:
    """Availability of the fused CLI, plus whether/how it can be installed.

    `installable` means POST /api/deploy/install can be expected to work:
    this interpreter satisfies the pinned wheel's python_version >= 3.11
    marker and has pip (the install lands in THIS interpreter, which is the
    one autodetected source — see fused_cli). When not installable, `reason`
    says why and `install_hint` names the manual command.
    """
    cli = fused_cli()
    python_ok = sys.version_info >= FUSED_MIN_PYTHON
    pip_ok = _pip_available()
    reason = None
    if cli is None:
        if not python_ok:
            reason = (
                f"the fused package needs Python 3.11+ (this server runs "
                f"{sys.version_info.major}.{sys.version_info.minor})"
            )
        elif not pip_ok:
            reason = (
                "this server's Python has no pip module (an embedded or packaged "
                "interpreter), so it can't install packages into itself; point "
                "FUSED_RENDER_FUSED_BIN at a `fused` executable installed with "
                "another Python"
            )
    return {
        "found": cli is not None,
        "command": " ".join(cli.command) if cli else None,
        "installable": cli is None and python_ok and pip_ok,
        "reason": reason,
        "install_hint": 'pip install "fused-render[fused]"',
    }


def install_fused() -> dict:
    """pip-install the pinned fused wheel into this server's interpreter.

    Raises DeployError with pip's tail on failure. The console script lands in
    the venv's bin/, where fused_command() step 2 finds it — no restart needed.
    """
    if sys.version_info < FUSED_MIN_PYTHON:
        raise DeployError(
            "the fused package needs Python 3.11+; this server runs "
            f"{sys.version_info.major}.{sys.version_info.minor} — recreate the venv on a "
            "newer Python, then pip install \"fused-render[fused]\""
        )
    if not _pip_available():
        raise DeployError(
            "this server's Python has no pip module, so it can't install packages into "
            "itself; install the fused CLI with a Python on your PATH and point "
            "FUSED_RENDER_FUSED_BIN at the `fused` executable if needed"
        )
    requirement = PINNED_FUSED_REQUIREMENT
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", requirement],
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise DeployError("pip install timed out; retry, or install manually") from None
    except OSError as e:
        raise DeployError(f"could not run pip: {e}") from None
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-8:])
        raise DeployError(f"pip install failed:\n{tail}")
    # The freshly-installed package must now be importable HERE — that is the
    # one autodetected CLI source (fused_cli step 2). Import-system finder
    # caches can lag a just-created site-packages dir; flush them first.
    importlib.invalidate_caches()
    if fused_cli() is None:
        raise DeployError(
            "pip install succeeded but the fused package is still not importable in "
            "the server's interpreter; check the install output / venv"
        )
    return {"ok": True, "requirement": requirement}


# -- environments (the fused CLI's own store) ---------------------------------


def _envs_file() -> str:
    # The same override the fused CLI itself honors (environments.py), so a
    # relocated store stays consistent between the CLI and this reader.
    return os.environ.get("OPENFUSED_ENVS_FILE") or os.path.expanduser("~/.openfused/envs.json")


def eligible_envs() -> dict:
    """Hosted envs from the fused store + the picker's default.

    Reads ~/.openfused/envs.json directly (like the flow app's readEnvs) so
    the picker renders even when the CLI is not installed yet. Default pick:
    OPENFUSED_ENV when it names an eligible env (explicit intent for this
    process), else the first `fused`-backend env — preferring the store's own
    default when that is one — else the store default, else the first eligible.
    """
    data = storage.read_json(_envs_file())
    raw_envs = data.get("envs") if isinstance(data, dict) else None
    envs = []
    if isinstance(raw_envs, dict):
        for entry in raw_envs.values():
            if not isinstance(entry, dict):
                continue
            name, backend = entry.get("name"), entry.get("backend")
            if isinstance(name, str) and backend in HOSTED_BACKENDS:
                envs.append({"name": name, "backend": backend})
    envs.sort(key=lambda e: e["name"])

    by_name = {e["name"]: e for e in envs}
    store_default = data.get("default") if isinstance(data, dict) else None
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

    return {"envs": envs, "default_env": default, "envs_file": _envs_file()}


def _backend_of(env_name: str) -> str | None:
    for e in eligible_envs()["envs"]:
        if e["name"] == env_name:
            return e["backend"]
    return None


# -- `fused share …` execution -------------------------------------------------


def _cli_error(stderr: str, fallback: str) -> str:
    """Last non-empty stderr line with click's `Error: ` prefix stripped — the
    CLI's messages already name the fix, so they reach the modal verbatim."""
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    message = lines[-1] if lines else fallback
    return message.removeprefix("Error: ")


def _child_env(cli: FusedCli, env_name: str) -> dict[str, str]:
    """The child environment for a fused CLI run.

    OPENFUSED_ENV targets the chosen env (the CLI's own override channel).
    For an EXTERNAL cli (FUSED_RENDER_FUSED_BIN), interpreter-scoped vars are
    scrubbed: inside the packaged macOS app the process carries PYTHONHOME/
    PYTHONPATH pointing into the bundle, which would break any other Python's
    interpreter (same scrub the las template does for its external spawns).
    The in-interpreter shim keeps them — they are what make sys.executable
    work in the bundle.
    """
    child = {**os.environ, "OPENFUSED_ENV": env_name}
    if cli.external:
        for var in ("PYTHONHOME", "PYTHONPATH"):
            child.pop(var, None)
    return child


def _run_share(env_name: str, args: list[str], timeout: float = SHARE_TIMEOUT):
    """Run `fused share <args>` against `env_name` and parse its JSON stdout.

    Every `share` verb prints structured JSON (the CLI's _out); human notes go
    to stderr.
    """
    cli = fused_cli()
    if cli is None:
        raise DeployError(
            "the fused CLI is not available: the fused package is not importable in "
            "the server's environment and no FUSED_RENDER_FUSED_BIN override is set; "
            "install it from the Deploy dialog or run: pip install \"fused-render[fused]\""
        )
    try:
        proc = subprocess.run(
            [*cli.command, "share", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_child_env(cli, env_name),
        )
    except subprocess.TimeoutExpired:
        raise DeployError(f"`fused share {args[0]}` timed out after {int(timeout)}s") from None
    except OSError as e:
        raise DeployError(f"could not run the fused CLI ({cli.command[0]}): {e}") from None
    if proc.returncode != 0:
        raise DeployError(_cli_error(proc.stderr, f"fused share {args[0]} failed"))
    try:
        return json.loads(proc.stdout)
    except ValueError:
        tail = proc.stdout.strip()[-200:]
        raise DeployError(
            f"the fused CLI printed something that wasn't JSON: {tail!r}"
        ) from None


def _list_mounts(env_name: str) -> list[dict]:
    """`share list --all` on env — the authoritative record of what is live.

    --all so the read isn't owner-scoped on AWS (an AWS_PROFILE/role change
    would otherwise make our own mount vanish and reconcile to a false
    revoked); it is a documented no-op on the managed fused backend.
    """
    parsed = _run_share(env_name, ["list", "--all"], timeout=LIST_TIMEOUT)
    return [m for m in parsed if isinstance(m, dict)] if isinstance(parsed, list) else []


def _find_mount(mounts: list[dict], token: str) -> dict | None:
    # The managed backend keys by id, AWS by token; a pointer may hold either.
    for m in mounts:
        if token in (m.get("token"), m.get("id")):
            return m
    return None


def _classify_mount(mounts: list[dict], token: str) -> str:
    """"active" | "revoked" | "absent" — absent (not in the list at all, e.g.
    after an infra teardown) is distinct from a revoked tombstone: repoint and
    recreate both fail on it, so the caller must fall through to create."""
    mount = _find_mount(mounts, token)
    if mount is None:
        return "absent"
    return "revoked" if mount.get("status") == "revoked" else "active"


# -- the per-page deployment pointer store ------------------------------------


def _store_path() -> str:
    return os.path.join(storage.home_dir(), "deployments.json")


def _load_store() -> dict:
    data = storage.read_json(_store_path())
    return data if isinstance(data, dict) else {}


def get_deployment(page: str) -> dict | None:
    record = _load_store().get(page)
    return record if isinstance(record, dict) else None


def set_deployment(page: str, record: dict) -> None:
    store = _load_store()
    store[page] = record
    storage.write_json(_store_path(), store)


# -- deploy orchestration ------------------------------------------------------


def _record_from(raw: dict, *, page: str, env_name: str, backend: str,
                 entrypoints: list[str], fallback: dict | None) -> dict:
    token = raw.get("token") or raw.get("id") or (fallback or {}).get("token")
    if not isinstance(token, str) or not token:
        raise DeployError("the fused CLI did not return a mount token")
    # Only create/repoint/recreate ever return a URL, and only on the managed
    # backend (AWS prints token+path only) — keep the last-known URL rather
    # than regressing a previously-shown link to null. But ONLY while the
    # token is unchanged: a fresh create that minted a NEW token (absent mount
    # on an AWS env) must not pair the old URL with it — copy/open would point
    # at a link that no longer matches the live mount.
    url = raw.get("url")
    if not isinstance(url, str) or not url:
        same_token = (fallback or {}).get("token") == token
        url = (fallback or {}).get("url") if same_token else None
    return {
        "page": page,
        "env": env_name,
        "backend": backend,
        "token": token,
        "url": url if isinstance(url, str) else None,
        "status": "active",
        "entrypoints": entrypoints,
        "updated_at": _now_iso(),
    }


def preview_deploy(page: str) -> dict:
    """What a deploy of `page` would publish, resolved fresh — no files written.

    The modal shows this BEFORE the Deploy click (the flow app's
    DeployPreview precedent): the page itself, each runPython target and the
    route it becomes, each rawUrl/readFile asset — and any export blockers,
    so an unexportable page reads as "fix these" up front instead of a failed
    deploy. Same scan the real export runs (export.plan_export).
    """
    if not os.path.isfile(page):
        raise DeployError(f"no such file: {page}")
    if os.path.splitext(page)[1].lower() not in (".html", ".htm"):
        raise DeployError(f"{page} is not an .html/.htm page")
    try:
        with open(page, "r", encoding="utf-8", errors="replace") as f:
            html = f.read()
    except OSError as e:
        raise DeployError(f"cannot read {page}: {e}") from None
    plan = plan_export(html, os.path.dirname(os.path.abspath(page)))
    return {
        "page": os.path.basename(page),
        "entrypoints": [{"path": e.path, "name": e.name} for e in plan.entrypoints],
        "assets": [{"path": a.path, "name": a.name} for a in plan.assets],
        "errors": plan.errors,
    }


def deploy_page(page: str, env_name: str) -> dict:
    """Export `page` to a temp bundle and publish it on `env_name`; returns the
    stored deployment record (token, URL when the backend returned one).

    First deploy (or a pointer on a different env): `share create --public` —
    a fresh opaque capability URL. Redeploy on the same env keeps the URL:
    active mount -> `share repoint <token>`; revoked tombstone -> `share
    recreate --same-token` then repoint (same URL comes back); token absent
    from `share list` entirely -> fresh create (nothing left to revive).
    """
    backend = _backend_of(env_name)
    if backend is None:
        raise DeployError(
            f"{env_name!r} is not a hosted environment (backends: fused, aws) — "
            "pick one from the Deploy dialog"
        )

    bundle = tempfile.mkdtemp(prefix="fused-render-deploy-")
    try:
        plan = export_page(page, bundle)
        entrypoints = [e.name for e in plan.entrypoints]

        pointer = get_deployment(page)
        same_env = pointer if pointer and pointer.get("env") == env_name else None
        token = same_env.get("token") if same_env else None

        if not token:
            raw = _run_share(env_name, ["create", bundle, "--public"])
        else:
            live = _classify_mount(_list_mounts(env_name), token)
            if live == "active":
                raw = _run_share(env_name, ["repoint", token, bundle])
            elif live == "revoked":
                _run_share(env_name, ["recreate", token, "--same-token"])
                try:
                    raw = _run_share(env_name, ["repoint", token, bundle])
                except DeployError:
                    # The revive succeeded but the publish didn't: best-effort
                    # re-revoke so a deliberately taken-down link doesn't come
                    # back silently live with its OLD content.
                    try:
                        _run_share(env_name, ["revoke", token])
                    except DeployError:
                        pass
                    raise
            else:  # absent — e.g. after an infra teardown; nothing to revive
                raw = _run_share(env_name, ["create", bundle, "--public"])

        record = _record_from(
            raw, page=page, env_name=env_name, backend=backend,
            entrypoints=entrypoints, fallback=same_env,
        )
        set_deployment(page, record)
        return record
    finally:
        shutil.rmtree(bundle, ignore_errors=True)


def revoke_deployment(page: str) -> dict:
    """`share revoke` the page's mount; the pointer flips to revoked (kept, not
    cleared — a later deploy revives the same token/URL)."""
    pointer = get_deployment(page)
    if pointer is None or not pointer.get("token") or not pointer.get("env"):
        raise DeployError("this page has no recorded deployment to revoke")
    _run_share(pointer["env"], ["revoke", pointer["token"]])
    record = {**pointer, "status": "revoked", "updated_at": _now_iso()}
    set_deployment(page, record)
    return record


def revoke_mount(env_name: str, token: str) -> dict:
    """`share revoke` a mount by token — the Preferences page's revoke, which
    also covers mounts with no local pointer (deployed by the CLI, another
    app, or another machine; the CLI's owner-binding still applies and its
    refusal surfaces verbatim). Any local pointer recording this mount flips
    to revoked so per-page state stays consistent.

    One mount can be addressed by its token OR its id (the managed backend
    carries both — _find_mount's dual matching): a pointer stored whichever
    the create output carried, while the share-list row may show the other.
    So the mount's aliases are collected from `share list` BEFORE the revoke
    and the pointer flip matches any of them — a pointer must never stay
    "active" (stale dot, wrong modal state) for a link that was just taken
    down. Best-effort: an unreadable list degrades to the given token alone.
    """
    aliases = {token}
    try:
        mount = _find_mount(_list_mounts(env_name), token)
    except DeployError:
        mount = None
    if mount is not None:
        for key in ("token", "id"):
            value = mount.get(key)
            if isinstance(value, str) and value:
                aliases.add(value)
    _run_share(env_name, ["revoke", token])
    store = _load_store()
    changed = False
    for page, record in store.items():
        if (
            isinstance(record, dict)
            and record.get("env") == env_name
            and record.get("token") in aliases
            and record.get("status") != "revoked"
        ):
            store[page] = {**record, "status": "revoked", "updated_at": _now_iso()}
            changed = True
    if changed:
        storage.write_json(_store_path(), store)
    return {"env": env_name, "token": token, "status": "revoked"}


def deployment_status(page: str, reconcile: bool) -> dict:
    """The stored pointer; with reconcile=True its status is checked against
    `share list` (truth) so an out-of-band CLI revoke/recreate shows through.
    An unreachable env returns the last-known pointer with reconciled=False
    rather than failing the whole dialog.

    A reconciled response also carries ``live``: the mount's raw `share list`
    classification (active | revoked | absent). The persisted pointer status
    stays binary — absent persists as "revoked" (the link IS down) — but the
    distinction matters to the modal: a revoked tombstone redeploys to the
    SAME URL (recreate --same-token), while an absent mount (e.g. after an
    infra teardown) gets a fresh create and a NEW link, so the button must
    not promise a restore it can't deliver. ``live`` is null when the check
    didn't run.
    """
    pointer = get_deployment(page)
    if pointer is None:
        return {"deployment": None, "reconciled": True, "live": None}
    if not reconcile or not pointer.get("env") or not pointer.get("token"):
        return {"deployment": pointer, "reconciled": False, "live": None}
    try:
        mounts = _list_mounts(pointer["env"])
    except DeployError:
        return {"deployment": pointer, "reconciled": False, "live": None}
    live = _classify_mount(mounts, pointer.get("token", ""))
    status = "active" if live == "active" else "revoked"
    if status != pointer.get("status"):
        pointer = {**pointer, "status": status, "updated_at": _now_iso()}
        set_deployment(page, pointer)
    return {"deployment": pointer, "reconciled": True, "live": live}


def _serve_base_url(env_name: str) -> str | None:
    """The env's serving-plane base URL, derived from any stored pointer.

    `share list` never returns URLs (either backend) — but every mount on one
    env is served under one base as ``<base>/<token>`` (the fused repo's
    spec/serve/share-links.md §6), so a single recorded absolute URL whose
    path ends in its own token yields the base for every other token on that
    env. Best-effort: None when no such pointer exists yet.
    """
    for record in _load_store().values():
        if not isinstance(record, dict) or record.get("env") != env_name:
            continue
        url, token = record.get("url"), record.get("token")
        if isinstance(url, str) and isinstance(token, str) and token:
            trimmed = url.rstrip("/")
            if trimmed.endswith("/" + token):
                return trimmed[: -len(token)]
    return None


def list_shares(env_name: str) -> dict:
    """Every mount on `env_name` (`share list --all`), joined back to local
    pages via the pointer store — the "which of my files is deployed" view.
    A mount with no matching pointer (another app/machine, or the CLI) has
    page=null. `share list` carries no URLs, so each mount's URL is the
    pointer's recorded one, else derived from the env's base URL
    (_serve_base_url) when a recorded link reveals it."""
    mounts = _list_mounts(env_name)
    by_token: dict[str, dict] = {}
    for page, record in _load_store().items():
        if isinstance(record, dict) and record.get("env") == env_name and record.get("token"):
            by_token[record["token"]] = {"page": page, "record": record}
    base = _serve_base_url(env_name)

    out = []
    for m in mounts:
        token = m.get("token") or m.get("id")
        if not isinstance(token, str):
            continue
        hit = by_token.get(m.get("token") or "") or by_token.get(m.get("id") or "")
        url = m.get("url")
        if not isinstance(url, str) or not url:
            url = hit["record"].get("url") if hit else None
        if (not isinstance(url, str) or not url) and base:
            url = base + token
        out.append(
            {
                "token": token,
                "status": m.get("status") or "active",
                "type": m.get("type"),
                "url": url if isinstance(url, str) else None,
                "page": hit["page"] if hit else None,
            }
        )
    # Local pages first, then live before revoked, then by token — stable.
    out.sort(key=lambda m: (m["page"] is None, m["status"] == "revoked", m["token"]))
    return {"env": env_name, "mounts": out}


# -- routes --------------------------------------------------------------------


def _setup_cli_hint() -> str:
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


@router.get("/api/deploy/config")
def api_deploy_config():
    return {"cli": cli_status(), "setup_cli": _setup_cli_hint(), **eligible_envs()}


@router.get("/api/deploy/status")
def api_deploy_status(path: str, reconcile: str = "0"):
    if not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    return deployment_status(path, reconcile == "1")


@router.get("/api/deploy/preview")
def api_deploy_preview(path: str):
    if not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    try:
        return preview_deploy(path)
    except DeployError as e:
        return _error(str(e))


@router.get("/api/deploy/shares")
def api_deploy_shares(env: str):
    try:
        return list_shares(env)
    except DeployError as e:
        return _error(str(e))


@router.post("/api/deploy")
def api_deploy(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    page = body.get("page")
    env_name = body.get("env")
    if not page or not os.path.isabs(page):
        return _error("'page' must be an absolute path to the .html page")
    if not env_name or not isinstance(env_name, str):
        return _error("'env' must name a hosted environment")
    try:
        return deploy_page(page, env_name)
    except ExportError as e:
        return _error(str(e))
    except DeployError as e:
        return _error(str(e))


@router.post("/api/deploy/revoke")
def api_deploy_revoke(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    # Two addressing modes: by page (the Deploy modal — the page's own
    # pointer) or by env+token (the Preferences page's share list, which
    # also covers mounts with no local pointer).
    page = body.get("page")
    env_name, token = body.get("env"), body.get("token")
    if isinstance(env_name, str) and env_name and isinstance(token, str) and token:
        try:
            return revoke_mount(env_name, token)
        except DeployError as e:
            return _error(str(e))
    if not page or not os.path.isabs(page):
        return _error("provide 'page' (absolute path) or 'env' + 'token'")
    try:
        return revoke_deployment(page)
    except DeployError as e:
        return _error(str(e))


@router.post("/api/deploy/install")
def api_deploy_install(x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        return install_fused()
    except DeployError as e:
        return _error(str(e))
