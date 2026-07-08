"""Deploy a renderable page to a hosted environment through the `fused` CLI.

Export (export.py) stops at a local bundle directory; this module is the next
step: it turns "I have a page" into "I have a URL", by orchestrating the
already-implemented `fused share` CLI group (the fused repo,
spec/serve/share-links.md + spec/serve/fused-render.md). fused-render itself
still hosts nothing and mints nothing — every mutation is a `fused share …`
child process, exactly the pattern the flow app uses for project deploys
(flow repo, spec/app/deploy-project.md). What this module owns:

  * **The fused CLI seam.** Deploying needs the `fused` package, which may not
    be installed in the venv running this server. `fused_command()` resolves
    the CLI (FUSED_RENDER_FUSED_BIN override -> the server venv's own bin/ ->
    PATH), and `POST /api/deploy/install` pip-installs the wheel pinned by
    this package's own `[fused]` extra into the server's venv when it is
    missing (Python 3.11+, matching the extra's marker).
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

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.export import ExportError, export_page
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


def fused_command() -> list[str] | None:
    """The command vector that runs the fused CLI, or None when not found.

    Resolution order:
      1. FUSED_RENDER_FUSED_BIN — trusted verbatim, split on whitespace so a
         compound command works (e.g. "uv run fused"). Mirrors the flow app's
         OPENFUSED_BIN seam; it is also how tests substitute a stub CLI.
      2. a `fused` console script in the same bin/ dir as this interpreter —
         the venv running the server, where /api/deploy/install lands it.
      3. `fused` on PATH.
    """
    override = os.environ.get("FUSED_RENDER_FUSED_BIN")
    if override:
        parts = override.split()
        return parts if parts else None
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    for name in ("fused", "fused.exe"):
        candidate = os.path.join(exe_dir, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return [candidate]
    which = shutil.which("fused")
    return [which] if which else None


def _pinned_fused_requirement() -> str | None:
    """The requirement string this package's own `[fused]` extra pins.

    Read from installed metadata (importlib.metadata.requires) rather than
    hardcoding the wheel URL here — pyproject.toml stays the single source of
    the pin. Returns the requirement without its environment marker (pip gets
    the plain spec), or None when the metadata is unavailable (e.g. running
    from a source tree that was never pip-installed).
    """
    import importlib.metadata

    try:
        requires = importlib.metadata.requires("fused-render") or []
    except importlib.metadata.PackageNotFoundError:
        return None
    for spec in requires:
        if "extra" in spec and 'extra == "fused"' in spec:
            return spec.split(";", 1)[0].strip()
    return None


def cli_status() -> dict:
    """Availability of the fused CLI, plus whether/how it can be installed.

    `installable` means POST /api/deploy/install can be expected to work: a
    pinned requirement is known and this interpreter satisfies the extra's
    python_version >= 3.11 marker. When not installable, `reason` says why and
    `install_hint` names the manual command.
    """
    command = fused_command()
    requirement = _pinned_fused_requirement()
    python_ok = sys.version_info >= (3, 11)
    reason = None
    if command is None:
        if not python_ok:
            reason = (
                f"the fused package needs Python 3.11+ (this server runs "
                f"{sys.version_info.major}.{sys.version_info.minor})"
            )
        elif requirement is None:
            reason = (
                "fused-render's package metadata is unavailable, so the pinned fused "
                "wheel can't be determined; install manually"
            )
    return {
        "found": command is not None,
        "command": " ".join(command) if command else None,
        "installable": command is None and python_ok and requirement is not None,
        "reason": reason,
        "install_hint": 'pip install "fused-render[fused]"',
    }


def install_fused() -> dict:
    """pip-install the pinned fused wheel into this server's interpreter.

    Raises DeployError with pip's tail on failure. The console script lands in
    the venv's bin/, where fused_command() step 2 finds it — no restart needed.
    """
    if sys.version_info < (3, 11):
        raise DeployError(
            "the fused package needs Python 3.11+; this server runs "
            f"{sys.version_info.major}.{sys.version_info.minor} — recreate the venv on a "
            "newer Python, then pip install \"fused-render[fused]\""
        )
    requirement = _pinned_fused_requirement()
    if requirement is None:
        raise DeployError(
            "cannot determine the pinned fused wheel from fused-render's package "
            'metadata; install manually: pip install "fused-render[fused]"'
        )
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
    if fused_command() is None:
        raise DeployError(
            "pip install succeeded but no `fused` executable was found afterwards; "
            "check the server's PATH/venv"
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


def _run_share(env_name: str, args: list[str], timeout: float = SHARE_TIMEOUT):
    """Run `fused share <args>` against `env_name` and parse its JSON stdout.

    Every `share` verb prints structured JSON (the CLI's _out); human notes go
    to stderr. The env is targeted via OPENFUSED_ENV on the child — the CLI's
    own override channel, no config file edited.
    """
    command = fused_command()
    if command is None:
        raise DeployError(
            "the fused CLI is not installed in the server's environment; install it "
            "from the Deploy dialog or run: pip install \"fused-render[fused]\""
        )
    try:
        proc = subprocess.run(
            [*command, "share", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "OPENFUSED_ENV": env_name},
        )
    except subprocess.TimeoutExpired:
        raise DeployError(f"`fused share {args[0]}` timed out after {int(timeout)}s") from None
    except OSError as e:
        raise DeployError(f"could not run the fused CLI ({command[0]}): {e}") from None
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
    # than regressing a previously-shown link to null.
    url = raw.get("url")
    if not isinstance(url, str) or not url:
        url = (fallback or {}).get("url")
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


def deployment_status(page: str, reconcile: bool) -> dict:
    """The stored pointer; with reconcile=True its status is checked against
    `share list` (truth) so an out-of-band CLI revoke/recreate shows through.
    An unreachable env returns the last-known pointer with reconciled=False
    rather than failing the whole dialog."""
    pointer = get_deployment(page)
    if pointer is None:
        return {"deployment": None, "reconciled": True}
    if not reconcile or not pointer.get("env") or not pointer.get("token"):
        return {"deployment": pointer, "reconciled": False}
    try:
        mounts = _list_mounts(pointer["env"])
    except DeployError:
        return {"deployment": pointer, "reconciled": False}
    live = "active" if _classify_mount(mounts, pointer.get("token", "")) == "active" else "revoked"
    if live != pointer.get("status"):
        pointer = {**pointer, "status": live, "updated_at": _now_iso()}
        set_deployment(page, pointer)
    return {"deployment": pointer, "reconciled": True}


def list_shares(env_name: str) -> dict:
    """Every mount on `env_name` (`share list --all`), joined back to local
    pages via the pointer store — the "which of my files is deployed" view.
    A mount with no matching pointer (CLI-created, another machine) has
    page=null; neither backend's list carries a URL, so the pointer's
    last-known one is used when available."""
    mounts = _list_mounts(env_name)
    by_token: dict[str, dict] = {}
    for page, record in _load_store().items():
        if isinstance(record, dict) and record.get("env") == env_name and record.get("token"):
            by_token[record["token"]] = {"page": page, "record": record}

    out = []
    for m in mounts:
        token = m.get("token") or m.get("id")
        if not isinstance(token, str):
            continue
        hit = by_token.get(m.get("token") or "") or by_token.get(m.get("id") or "")
        url = m.get("url")
        if not isinstance(url, str) or not url:
            url = hit["record"].get("url") if hit else None
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


@router.get("/api/deploy/config")
def api_deploy_config():
    return {"cli": cli_status(), **eligible_envs()}


@router.get("/api/deploy/status")
def api_deploy_status(path: str, reconcile: str = "0"):
    if not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    return deployment_status(path, reconcile == "1")


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
    page = body.get("page")
    if not page or not os.path.isabs(page):
        return _error("'page' must be an absolute path to the .html page")
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
