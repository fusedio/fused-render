"""GET/PUT /api/prefs — user preferences at ~/.fused-render/prefs.json.

The Preferences page's backend (SPEC §20): a tiny persisted preference store
(shell/storage, beside bookmarks.json/deployments.json) plus the derived,
read-only facts the page shows next to it (log location, engine
availability/forcing).

The one persisted preference today is the **execution engine** for /api/run:

  * ``"builtin"`` (default) — the built-in executor: fresh subprocess per
    call, the environment that launched the server (D70's builtin-by-default
    stands; the pref is the opt-in D69 anticipated).
  * ``"fused"`` — the fused local compute backend (engine.py): PEP 723 inline
    requirements resolved into cached venvs, ``@fused.udf``/``result``
    entrypoints. Selecting it is effective only while the ``fused`` package
    is importable (``fused_engine_available``); otherwise execution falls
    back to builtin and the page says so.

The preference is read per request (server.py's /api/run dispatch), so a
switch applies to the next run with no restart — the same no-restart
discipline as the template registries (CT-5). The ``FUSED_RENDER_ENGINE``
environment variable stays the *process-level* override: when set it wins
over the pref entirely (server.py validates it at startup), and the page
shows the pref as locked.

No import of server.py (server includes this router — keep it acyclic); the
X-Fused guard and the small env-var effective-engine mirror are duplicated
locally like shell/bookmarks.py's guard is.
"""
import os

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.logs import log_dir, log_path
from fused_render.shell import storage

router = APIRouter()

VALID_ENGINES = ("builtin", "fused")


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Same D3 guard as server._require_fused, duplicated to keep shell↛server
    # acyclic (see shell/bookmarks.py).
    if x_fused != "1":
        return JSONResponse({"error": "missing X-Fused header"}, status_code=403)
    return None


def _path() -> str:
    return os.path.join(storage.home_dir(), "prefs.json")


def read_prefs() -> dict:
    data = storage.read_json(_path())
    return data if isinstance(data, dict) else {}


def selected_engine() -> str:
    """The persisted engine preference; unset/unknown values read as builtin."""
    value = read_prefs().get("engine")
    return value if value in VALID_ENGINES else "builtin"


def fused_engine_available() -> bool:
    """Whether the fused local compute backend is importable right now.

    Probed per call (not cached): /api/deploy/install can land the package
    mid-session, and the page should reflect that without a restart.
    """
    try:
        from fused_render import engine as _engine
    except ImportError:
        return False
    return _engine.available()


def effective_engine() -> str:
    """The engine an *unforced* /api/run would use right now: the selected
    pref, degraded to builtin while the fused backend isn't importable."""
    if selected_engine() == "fused" and fused_engine_available():
        return "fused"
    return "builtin"


def engine_state() -> dict:
    """The engine block of GET /api/prefs.

    ``forced_by`` is the raw FUSED_RENDER_ENGINE value when set — the process
    override that beats the pref (server.py validated it at startup; the
    small auto/fused → effective mirror here is display-only and matches
    server._forced_engine's resolution).
    """
    forced_raw = os.environ.get("FUSED_RENDER_ENGINE")
    available = fused_engine_available()
    selected = selected_engine()
    if forced_raw is not None:
        requested = forced_raw.strip().lower()
        effective = "fused" if (requested == "fused" or (requested == "auto" and available)) else "builtin"
    else:
        effective = "fused" if (selected == "fused" and available) else "builtin"
    return {
        "selected": selected,
        "effective": effective,
        "forced_by": forced_raw,
        "fused_available": available,
    }


@router.get("/api/prefs")
def get_prefs():
    return {
        "engine": engine_state(),
        # Where this process is logging (logs.py): the page's "open the logs
        # location" action reveals `path` via the existing /api/fs/reveal.
        "log": {"path": log_path(), "dir": log_dir()},
    }


@router.put("/api/prefs")
def put_prefs(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    engine = body.get("engine")
    if engine not in VALID_ENGINES:
        return JSONResponse(
            {"error": f"'engine' must be one of: {', '.join(VALID_ENGINES)}"}, status_code=400
        )
    prefs = read_prefs()
    prefs["engine"] = engine
    storage.write_json(_path(), prefs)
    # The new state, so the page re-renders from the response (the pref is
    # persisted even while FUSED_RENDER_ENGINE forces — it applies once the
    # override is removed; the response's forced_by says so).
    return {"engine": engine_state(), "log": {"path": log_path(), "dir": log_dir()}}
