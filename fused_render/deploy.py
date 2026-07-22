"""Deploy a renderable page to a hosted environment through the `fused` CLI.

Export (export.py) stops at a local bundle directory; this module is the next
step: it turns "I have a page" into "I have a URL", by orchestrating the
already-implemented `fused share` CLI group (the fused repo,
spec/serve/share-links.md + spec/serve/fused-render.md). fused-render itself
still hosts nothing and mints nothing — every mutation is a `fused share …`
child process, exactly the pattern the flow app uses for project deploys
(flow repo, spec/app/deploy-project.md). What this module owns:

  * **The one-click install.** Deploying needs the `fused` package, which may
    not be installed in the venv running this server; the resolution seam
    itself (`fused_cli()` — an explicit FUSED_RENDER_FUSED_BIN override, or
    the ONE autodetected source, the `fused` package importable in this
    interpreter via the `_fused_cli.py` shim) lives in fusedcli.py, shared
    with the account surface (account.py). `POST /api/deploy/install`
    pip-installs the wheel pinned by `PINNED_FUSED_REQUIREMENT` into the
    server's interpreter when it is missing (Python 3.11+ with pip).
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

Deploys are **public share links** (`share create --public`): per
spec/serve/fused-render.md, authed mounts can't serve a hosted page's asset
GETs yet, so public is the one posture that works fully today. The token
itself defaults to an opaque, unguessable one (no --token), but the Deploy
dialog's "Link name" field lets the user pass an explicit --token instead —
a deliberately public, guessable URL (fused's own gate: --public + a chosen
--token is allowed, unlike --public + an auth gate).

No import of server.py (server imports this router — keep it acyclic); the
X-Fused guard is duplicated locally like shell/bookmarks.py does.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.export import ExportError, export_page, plan_export

# The fused CLI seam (resolution, child-env hygiene, error mapping, the CLI's
# own on-disk state) is shared with account.py — it lives in fusedcli.py.
# Private aliases keep this module's historical names (and test patch targets)
# stable.
from fused_render.fusedcli import (
    child_env as _child_env,
    cli_error as _cli_error,
    eligible_envs,
    fused_cli,
    fused_cloud_logged_in,
    setup_cli_hint as _setup_cli_hint,
)
from fused_render.shell import storage

router = APIRouter()

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


def _str_list(body: dict, key: str) -> list[str] | None:
    """A JSON body field that must be a list of strings (absent/None -> []).

    Returns None when the field is present but malformed (not a list, or holds a
    non-string) so the caller can 400 rather than pass junk to the exporter."""
    value = body.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        return None
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# -- the one-click install (the CLI seam itself is fusedcli.py) ---------------


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
    "fused @ https://fused-magic.s3.us-west-2.amazonaws.com/fused-2.9.3.post10-py3-none-any.whl"
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


# -- environments (store reads live in fusedcli.py) ---------------------------


def _backend_of(env_name: str) -> str | None:
    for e in eligible_envs()["envs"]:
        if e["name"] == env_name:
            return e["backend"]
    return None


# -- `fused share …` execution -------------------------------------------------


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


# The whole store is rewritten on every mutation (write_json of the full
# dict). Starlette runs these sync endpoints in a threadpool, so two writers
# — including a focus-triggered reconcile in deployment_status, a hidden
# writer — can read-modify-write concurrently and lose one update. Serialize
# every mutation through one process lock to close that window.
_STORE_LOCK = threading.Lock()


def _store_path() -> str:
    return os.path.join(storage.home_dir(), "deployments.json")


def _load_store() -> dict:
    """The store for READS — lenient: a missing or corrupt file reads as empty
    (the page shows as not-deployed rather than erroring the whole preview)."""
    data = storage.read_json(_store_path())
    return data if isinstance(data, dict) else {}


def _load_store_for_write() -> dict:
    """The store for WRITES — refuses to silently overwrite a corrupt file.

    Each write persists the whole dict, so building it from `{}` after a parse
    failure would drop every OTHER page's pointer (orphaning still-live public
    mounts the app could no longer revoke). A missing file is a clean first
    write (`{}`); a file that exists but doesn't parse to a dict raises so the
    caller aborts and the user can move it aside — data is never destroyed.
    """
    path = _store_path()
    data = storage.read_json(path)
    if isinstance(data, dict):
        return data
    if os.path.exists(path):
        raise DeployError(
            f"{path} is not valid JSON — refusing to overwrite it (that would drop "
            "other pages' deployment records). Move the file aside and retry."
        )
    return {}


def _update_store(mutate) -> None:
    """Serialized, corruption-safe read-modify-write. `mutate(store)` edits the
    dict in place under the lock; the strict load never clobbers a corrupt
    file (see _load_store_for_write). The lock is held only across a local
    file read+write, never across a CLI subprocess."""
    with _STORE_LOCK:
        store = _load_store_for_write()
        mutate(store)
        storage.write_json(_store_path(), store)


def _page_key(page: str) -> str:
    """Canonical pointer-store key for a page path.

    `os.path.abspath` (not realpath) — it collapses `.`/`..`/redundant separators
    without resolving symlinks, matching what the exporter uses
    (`os.path.dirname(os.path.abspath(page))`). Without this, two spellings of the
    same file (`/a/b/../p.html` vs `/a/p.html`) would key different pointers, so
    status, the deploy dot, and redeploy could miss an existing deployment.
    """
    return os.path.abspath(page)


def get_deployment(page: str) -> dict | None:
    record = _load_store().get(_page_key(page))
    return record if isinstance(record, dict) else None


def set_deployment(page: str, record: dict) -> None:
    key = _page_key(page)
    _update_store(lambda store: store.__setitem__(key, record))


# -- deploy orchestration ------------------------------------------------------


def _record_from(raw: dict, *, page: str, env_name: str, backend: str,
                 entrypoints: list[str], include: list[str], exclude: list[str],
                 cache_max_age: str, named: bool, fallback: dict | None) -> dict:
    # `cache_max_age` here is simply what the caller requested this deploy — every
    # branch in deploy_page (create, repoint, recreate+repoint) now actually applies
    # it (`application` repo spec 021 §3.1, amended: a managed Fused mount's
    # cache_settings is changeable in place via repoint, not just fixed at first
    # create), so the record's stored value and the live mount agree on every path.
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
        # The file-selection the user deployed with, persisted here (not a separate
        # sidecar) so reopening the modal reloads exactly what was published: extra
        # files bundled beyond the auto-scan, and files dropped from it.
        "include": include,
        "exclude": exclude,
        # The caching choice deployed with (see deploy_page) — persisted the same way
        # as include/exclude so reopening the modal shows the current setting, and a
        # redeploy that doesn't touch it re-sends the same value (the fused CLI has no
        # "preserve on omit" for this — every deploy is an explicit, full statement).
        "cache_max_age": cache_max_age,
        # Whether this mount's token is a user-chosen name (a deliberately
        # guessable public URL) vs the default crypto-random opaque one. Set at
        # the fresh-create that minted the token and carried forward unchanged
        # on every token-reuse redeploy (repoint/recreate keep the token, so
        # they keep its provenance) — the modal shows "custom name" vs
        # "unguessable" from this without re-deriving it from the token string.
        "named": named,
        "updated_at": _now_iso(),
    }


def preview_deploy(
    page: str,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> dict:
    """What a deploy of `page` would publish, resolved fresh — no files written.

    The modal shows this BEFORE the Deploy click (the flow app's
    DeployPreview precedent): the page itself, each runPython target and the
    route it becomes, each rawUrl/readFile asset (plus the user's `include`,
    minus `exclude`) — plus any export blockers and advisory warnings, so an
    unexportable page reads as "fix these" up front instead of a failed deploy.
    Same scan the real export runs (export.plan_export), with the same selection.
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
    page_dir = os.path.dirname(os.path.abspath(page))
    plan = plan_export(html, page_dir, include=include, exclude=exclude)
    # The auto-detected set (the literal runPython/rawUrl/readFile scan, before any
    # include/exclude) — what the page publishes by DEFAULT. The modal uses it to
    # tell an auto-detected file (removing it means excluding it, and it belongs in
    # "Excluded" with a restore) from a purely manual include (removing it just
    # drops it). Cheap: a second pure regex scan over the same HTML.
    auto = plan_export(html, page_dir)
    return {
        "page": os.path.basename(page),
        "entrypoints": [{"path": e.path, "name": e.name} for e in plan.entrypoints],
        # `source` lets the "Will publish" list say HOW each asset is exposed: a
        # scanned literal rawUrl/readFile reference, a manifest-declared bundle file
        # (backs a computed path), or a hand-added include. See export.Asset.
        "assets": [{"path": a.path, "name": a.name, "source": a.source} for a in plan.assets],
        "auto": [e.path for e in auto.entrypoints] + [a.path for a in auto.assets],
        "errors": plan.errors,
        "warnings": plan.warnings,
    }


def deploy_page(
    page: str,
    env_name: str,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    cache_max_age: str = "0s",
    force_new: bool = False,
    custom_token: str | None = None,
) -> dict:
    """Export `page` to a temp bundle and publish it on `env_name`; returns the
    stored deployment record (token, URL when the backend returned one).

    `include`/`exclude` are the user's file selection (see export.plan_export):
    extra files bundled beyond the auto-scan, and files dropped from it. They are
    persisted on the record so a reopened modal reloads the same selection.

    `cache_max_age` (`"0s"` off by default, e.g. `"5m"`/`"1h"`) is the Deploy
    dialog's caching choice: how long a page's result may be served from cache
    instead of re-executed. It rides the export bundle's own manifest
    (`export.export_page`) AND is passed explicitly as `--cache-max-age` on
    every `share create`/`repoint` call — the two backends read it from
    different places (`fused` repo's spec/serve/fused-render.md § Caching): an
    AWS environment's `build_html_artifact` reads the manifest field (so either
    source works, including on a later `repoint`); a managed Fused environment
    reads it only from the explicit flag, as its own mount-level
    `cache_settings` field (`application` repo spec `021` §3.1), wholly
    independent of the bundle. `cache_settings` is no longer pinned for the
    life of a token — `repoint` (and a revoked-token revive's follow-up
    `repoint`) now carries `--cache-max-age` too, so a redeploy that reuses the
    token applies whatever `cache_max_age` this call requested, same as a fresh
    `create`. `force_new=True` still replaces the deployment outright — mints a
    fresh `share create` (a new token, new URL) with the requested
    `cache_max_age`, repoints this page's pointer to it, then **best-effort
    revokes the old mount** so the page is never left with two live URLs. The
    revoke is deliberately last (after the new mount is live) so a create
    failure never takes the page down, and best-effort (a revoke failure
    doesn't fail the deploy — the new URL is live and correct; the superseded
    mount lingers and is revocable from the Fused account page's deployments
    list). Because the pointer now tracks the new mount, the modal's Revoke
    button targets the new URL, as expected — there is no orphaned URL the user
    must chase separately.

    First deploy (or a pointer on a different env, or `force_new=True`):
    `share create --public` — a fresh opaque capability URL. Otherwise, redeploy
    on the same env keeps the URL: active mount -> `share repoint <token>`;
    revoked tombstone -> `share recreate --same-token` then repoint (same URL
    comes back); token absent from `share list` entirely -> fresh create
    (nothing left to revive).

    `custom_token`, when given, rides along as `share create`'s `--token` on
    every branch above that actually mints a FRESH mount (first deploy, a
    different env, or an absent mount) — it picks the URL's token instead of
    the default crypto-random opaque one (a deliberately public, guessable
    link — `fused`'s own `share create --public --token` gate, spec/serve/
    share-links.md §2). It is ignored on the two branches that redeploy an
    EXISTING mount (`repoint`, `recreate --same-token`): neither takes a token
    argument, because both keep the mount's original token, named or not —
    there is nothing to apply a new choice to. An already-taken or malformed
    name surfaces as the fused CLI's own error (via `_run_share`/DeployError).
    """
    include = include or []
    exclude = exclude or []
    # Canonicalize up front so the direct locked store.get below, the record's
    # `page` field, and set_deployment all key on the same path as get_deployment
    # / deployment_status (all via _page_key) — one file, one pointer.
    page = os.path.abspath(page)
    backend = _backend_of(env_name)
    if backend is None:
        raise DeployError(
            f"{env_name!r} is not a hosted environment (backends: fused, aws) — "
            "pick one from the Deploy dialog"
        )

    # Validate + snapshot the pointer under the lock BEFORE minting anything.
    # Validate: a corrupt file read leniently would look like "not deployed",
    # so a fresh `share create` would mint a mount and only the pointer write
    # afterward would discover the corruption (orphaning it) — fail fast.
    # The lock is released before the CLI runs (never held across a subprocess
    # / 120s network op). The read is then unavoidably a point-in-time
    # snapshot, but that is safe here:
    #   - the create-vs-repoint-vs-recreate decision below is driven by the
    #     LIVE `share list` classification (`_classify_mount`), NOT the stored
    #     status, so a concurrent reconcile/revoke — the only other writers,
    #     and both only flip `status` — cannot mis-drive it;
    #   - nothing deletes or re-keys a pointer; only a *second concurrent
    #     deploy of the SAME page* could change its token, and the modal
    #     disables Deploy while busy (one modal per page), so that is a UI
    #     invariant, not an API race we serialize here;
    #   - the final persist (set_deployment) is itself a locked read-modify-
    #     write touching only this page's key.
    with _STORE_LOCK:
        store = _load_store_for_write()
        pointer = store.get(page) if isinstance(store.get(page), dict) else None

    bundle = tempfile.mkdtemp(prefix="fused-render-deploy-")
    try:
        plan = export_page(page, bundle, include=include, exclude=exclude, cache_max_age=cache_max_age)
        entrypoints = [e.name for e in plan.entrypoints]

        # force_new deliberately treats any existing pointer as absent — never
        # reused for token lookup below — so the branch that follows always
        # takes the fresh-create path and mints a new token (see docstring).
        same_env = (
            None if force_new else (pointer if pointer and pointer.get("env") == env_name else None)
        )
        token = same_env.get("token") if same_env else None
        # On a force_new replace, the mount we're superseding (same page + env) —
        # revoked best-effort AFTER the new one is live, so the page never ends up
        # with two URLs the pointer can't both track. None unless force_new found a
        # prior same-env token.
        superseded_token = (
            pointer.get("token")
            if force_new and pointer and pointer.get("env") == env_name
            else None
        )
        # Token provenance for the stored record: a fresh create's is set by
        # whether a name was chosen this call; a token-reuse redeploy inherits
        # the existing record's (repoint/recreate keep the token, so its
        # named-ness is unchanged). Old records predating this field read as
        # False (unguessable) — the token can't be reclassified retroactively.
        named = bool(same_env.get("named")) if same_env else False
        if not token:
            named = bool(custom_token)
            create_args = ["create", bundle, "--public", "--cache-max-age", cache_max_age]
            if custom_token:
                create_args += ["--token", custom_token]
            raw = _run_share(env_name, create_args)
        else:
            live = _classify_mount(_list_mounts(env_name), token)
            # `--cache-max-age` on every branch below: AWS re-reads the bundle's own
            # manifest on every repoint anyway (so this is a no-op-equivalent
            # restatement of the same value), and on a managed Fused env `repoint`
            # now updates the mount's own `cache_settings` in place too
            # (`application` repo spec 021 §3.1, amended) — no fresh mount required
            # to change caching on a redeploy that reuses the token.
            if live == "active":
                raw = _run_share(
                    env_name, ["repoint", token, bundle, "--cache-max-age", cache_max_age]
                )
            elif live == "revoked":
                _run_share(env_name, ["recreate", token, "--same-token"])
                try:
                    raw = _run_share(
                        env_name, ["repoint", token, bundle, "--cache-max-age", cache_max_age]
                    )
                except DeployError as repoint_err:
                    # The revive (recreate) succeeded but the republish
                    # (repoint) failed — so the mount is live again with its
                    # OLD content. Best-effort take it back down, then persist
                    # the TRUE resulting state and raise an error that names it
                    # (the preview dot reads the pointer, so it must match
                    # reality either way).
                    try:
                        _run_share(env_name, ["revoke", token])
                    except DeployError:
                        # Couldn't take it down either: it is LIVE with previous
                        # content. Persist active (not the pre-deploy state —
                        # the dot would otherwise show it down while it serves)
                        # and tell the user a manual revoke may be needed.
                        set_deployment(
                            page, {**same_env, "status": "active", "updated_at": _now_iso()}
                        )
                        raise DeployError(
                            f"redeploy could not publish new content and then could not take "
                            f"the link down: mount {token!r} on {env_name!r} is LIVE with its "
                            f"previous content. Revoke it (Deploy dialog, or `fused share revoke "
                            f"{token}`) if that link must not stay up. Cause: {repoint_err}"
                        ) from repoint_err
                    # The compensating revoke landed — the link is down; reflect it.
                    set_deployment(
                        page, {**same_env, "status": "revoked", "updated_at": _now_iso()}
                    )
                    raise
            else:  # absent — e.g. after an infra teardown; nothing to revive
                named = bool(custom_token)  # a fresh create, like the first-deploy path
                create_args = ["create", bundle, "--public", "--cache-max-age", cache_max_age]
                if custom_token:
                    create_args += ["--token", custom_token]
                raw = _run_share(env_name, create_args)

        record = _record_from(
            raw, page=page, env_name=env_name, backend=backend,
            entrypoints=entrypoints, include=include, exclude=exclude,
            cache_max_age=cache_max_age, named=named, fallback=same_env,
        )
        set_deployment(page, record)
        # force_new replace: the new mount is live and the pointer now tracks it,
        # so take the superseded mount down — otherwise the page would serve at two
        # URLs while the pointer (and the modal's Revoke) tracks only the new one,
        # leaving the old one orphaned. Best-effort and LAST: the new URL is already
        # live and persisted, so a revoke failure must not fail the deploy — the
        # superseded mount just lingers and is revocable from the account page's
        # deployments list. Skip if the token didn't actually change (defensive: a
        # create that somehow returned the same token needs no self-revoke).
        if superseded_token and superseded_token != record["token"]:
            try:
                _run_share(env_name, ["revoke", superseded_token])
            except DeployError:
                # New deployment stands; the old mount outlives it until manually
                # revoked (account page / `fused share revoke <token>`). Not fatal.
                pass
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


def clear_cache_deployment(page: str) -> dict:
    """Clear every cached result for the page's deployed mount (`fused share
    cache-clear <token>`) — forces the next request to recompute instead of
    waiting out `cache_max_age`.

    Doesn't touch the mount record, its URL, or the deployment pointer: this is
    orthogonal to caching being on or off (it's a no-op cost if it was already
    off — nothing was cached). Returns the CLI's result verbatim
    (`{token, deleted, scope, prefix}`) so the modal can show what was cleared.
    """
    pointer = get_deployment(page)
    if pointer is None or not pointer.get("token") or not pointer.get("env"):
        raise DeployError("this page has no recorded deployment to clear the cache of")
    return _run_share(pointer["env"], ["cache-clear", pointer["token"]])


def revoke_mount(env_name: str, token: str) -> dict:
    """`share revoke` a mount by token — the account page's revoke, which
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

    def flip(store: dict) -> None:
        for page, record in store.items():
            if (
                isinstance(record, dict)
                and record.get("env") == env_name
                and record.get("token") in aliases
                and record.get("status") != "revoked"
            ):
                store[page] = {**record, "status": "revoked", "updated_at": _now_iso()}

    _update_store(flip)
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


def _serve_base_url(env_name: str, store: dict) -> str | None:
    """The env's serving-plane base URL, derived from any pointer in `store`.

    `share list` never returns URLs (either backend) — but every mount on one
    env is served under one base as ``<base>/<token>`` (the fused repo's
    spec/serve/share-links.md §6), so a single recorded absolute URL whose
    path ends in its own token yields the base for every other token on that
    env. Best-effort: None when no such pointer exists yet. Takes the
    already-loaded `store` so callers don't re-read deployments.json.
    """
    for record in store.values():
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
    store = _load_store()  # one read, shared with _serve_base_url below
    by_token: dict[str, dict] = {}
    for page, record in store.items():
        if isinstance(record, dict) and record.get("env") == env_name and record.get("token"):
            by_token[record["token"]] = {"page": page, "record": record}
    base = _serve_base_url(env_name, store)

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


# -- error viewing (`fused share errors`; the fused repo's error-reporting) ----


def _errors_args(
    token: str,
    err_id: str | None,
    *,
    limit: int,
    since: str | None,
    until: str | None,
    kind: str | None,
    entrypoint: str | None,
) -> list[str]:
    args = ["errors"]
    if err_id:
        # A single-record fetch (`errors TOKEN ERR_ID`) takes no list filters.
        # `--` terminates option parsing so a browser-supplied token/err_id that
        # begins with '-' can never be mis-parsed as a flag (e.g. token "--env"
        # silently flipping the per-mount list into an env-wide sweep).
        args += ["--", token, err_id]
        return args
    args += ["--limit", str(limit)]
    if since:
        args += ["--since", since]
    if until:
        args += ["--until", until]
    if kind:
        args += ["--kind", kind]
    if entrypoint:
        args += ["--entrypoint", entrypoint]
    # `--` before the positional token: see the err_id branch above.
    args += ["--", token]
    return args


def _run_errors(env_name: str, args: list[str]):
    try:
        return _run_share(env_name, args, timeout=LIST_TIMEOUT)
    except DeployError as e:
        # A fused CLI predating `share errors` exits with click's "No such
        # command 'errors'" — translate that to an upgrade hint the user can act
        # on, rather than surfacing a raw argument-parser error.
        msg = str(e).lower()
        if "no such command" in msg and "errors" in msg:
            raise DeployError(
                "this fused CLI is too old to read deployed errors — upgrade it "
                '(pip install -U "fused-render[fused]") so `fused share errors` exists.'
            ) from None
        raise


def list_errors(
    env_name: str,
    token: str,
    *,
    limit: int = 20,
    since: str | None = None,
    until: str | None = None,
    kind: str | None = None,
    entrypoint: str | None = None,
) -> dict:
    """Recent captured failures for one deployed mount, newest first
    (`fused share errors TOKEN`). Each item is a summary (id, time, entrypoint,
    kind, first error line); fetch the full record with `get_error`. This is the
    owner-only diagnostic channel — the deployed page's viewers never see it."""
    parsed = _run_errors(
        env_name,
        _errors_args(
            token,
            None,
            limit=limit,
            since=since,
            until=until,
            kind=kind,
            entrypoint=entrypoint,
        ),
    )
    errors = [e for e in parsed if isinstance(e, dict)] if isinstance(parsed, list) else []
    return {"env": env_name, "token": token, "errors": errors}


def get_error(env_name: str, token: str, err_id: str) -> dict:
    """One full error record by mount + id (`fused share errors TOKEN ERR_ID`) —
    the traceback, output tails, and params behind a deployed opaque 500."""
    parsed = _run_errors(
        env_name,
        _errors_args(
            token, err_id, limit=1, since=None, until=None, kind=None, entrypoint=None
        ),
    )
    if not isinstance(parsed, dict):
        raise DeployError("the fused CLI did not return an error record")
    return {"env": env_name, "token": token, "record": parsed}


# -- routes --------------------------------------------------------------------


@router.get("/api/deploy/config")
def api_deploy_config():
    return {
        "cli": cli_status(),
        "setup_cli": _setup_cli_hint(),
        # Managed-backend login signal (DP-2b): lets the modal warn BEFORE a
        # doomed deploy click when a `fused` env is targeted with no login.
        "fused_logged_in": fused_cloud_logged_in(),
        **eligible_envs(),
    }


@router.get("/api/deploy/status")
def api_deploy_status(path: str, reconcile: str = "0"):
    if not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    return deployment_status(path, reconcile == "1")


@router.post("/api/deploy/preview")
def api_deploy_preview(body: dict = Body(...)):
    # POST (not GET) because it carries the include/exclude selection — arrays
    # don't fit a query string cleanly. Read-only (no files written), so no
    # X-Fused guard, matching the former GET.
    path = body.get("path")
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    include = _str_list(body, "include")
    exclude = _str_list(body, "exclude")
    if include is None or exclude is None:
        return _error("'include'/'exclude' must be arrays of relative file paths")
    try:
        return preview_deploy(path, include, exclude)
    except DeployError as e:
        return _error(str(e))


@router.get("/api/deploy/shares")
def api_deploy_shares(env: str):
    try:
        return list_shares(env)
    except DeployError as e:
        return _error(str(e))


# Read-only diagnostics (no X-Fused guard, like /status and /shares): the owner's
# recent captured failures for a deployed mount, read through `fused share errors`.
@router.get("/api/deploy/errors")
def api_deploy_errors(
    env: str,
    token: str,
    limit: int = 20,
    since: str | None = None,
    until: str | None = None,
    kind: str | None = None,
    entrypoint: str | None = None,
):
    if not env or not token:
        return _error("'env' and 'token' are required")
    try:
        return list_errors(
            env,
            token,
            limit=max(1, min(limit, 100)),
            since=since,
            until=until,
            kind=kind,
            entrypoint=entrypoint,
        )
    except DeployError as e:
        return _error(str(e))


@router.get("/api/deploy/error")
def api_deploy_error(env: str, token: str, err_id: str):
    if not env or not token or not err_id:
        return _error("'env', 'token', and 'err_id' are required")
    try:
        return get_error(env, token, err_id)
    except DeployError as e:
        return _error(str(e))


@router.post("/api/deploy")
def api_deploy(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    page = body.get("page")
    env_name = body.get("env")
    # isinstance before os.path.isabs: a truthy non-string page (JSON number,
    # array) makes isabs() raise TypeError -> 500; the check keeps it a 400.
    if not isinstance(page, str) or not page or not os.path.isabs(page):
        return _error("'page' must be an absolute path to the .html page")
    if not env_name or not isinstance(env_name, str):
        return _error("'env' must name a hosted environment")
    include = _str_list(body, "include")
    exclude = _str_list(body, "exclude")
    if include is None or exclude is None:
        return _error("'include'/'exclude' must be arrays of relative file paths")
    cache_max_age = body.get("cache_max_age", "0s")
    if not isinstance(cache_max_age, str):
        return _error("'cache_max_age' must be a string, e.g. '0s'/'5m'/'1h'")
    force_new = body.get("force_new", False)
    if not isinstance(force_new, bool):
        return _error("'force_new' must be a boolean")
    # Optional chosen link name (a fresh-create-only knob — deploy_page ignores
    # it on a redeploy path that reuses an existing token). Format/uniqueness
    # aren't re-validated here: the fused CLI's own --token validation and
    # "already taken" check are the authority, and DeployError passes that
    # message through verbatim, same as every other CLI-side rejection.
    custom_token = body.get("token")
    if custom_token is not None:
        if not isinstance(custom_token, str) or not custom_token.strip():
            return _error("'token' must be a non-empty string")
        # Normalize at the boundary: forward the trimmed value, never the raw
        # one — otherwise "my-link " passes the non-empty check but reaches
        # `share create --token` with surrounding whitespace, which disagrees
        # with the client's TOKEN_RE and the CLI's own token rules.
        custom_token = custom_token.strip()
    try:
        return deploy_page(
            page,
            env_name,
            include,
            exclude,
            cache_max_age=cache_max_age,
            force_new=force_new,
            custom_token=custom_token,
        )
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
    # pointer) or by env+token (the account page's share list, which
    # also covers mounts with no local pointer).
    page = body.get("page")
    env_name, token = body.get("env"), body.get("token")
    if isinstance(env_name, str) and env_name and isinstance(token, str) and token:
        try:
            return revoke_mount(env_name, token)
        except DeployError as e:
            return _error(str(e))
    if not isinstance(page, str) or not page or not os.path.isabs(page):
        return _error("provide 'page' (absolute path) or 'env' + 'token'")
    try:
        return revoke_deployment(page)
    except DeployError as e:
        return _error(str(e))


@router.post("/api/deploy/clear-cache")
def api_deploy_clear_cache(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    page = body.get("page")
    if not isinstance(page, str) or not page or not os.path.isabs(page):
        return _error("'page' must be an absolute path to the .html page")
    try:
        return clear_cache_deployment(page)
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
