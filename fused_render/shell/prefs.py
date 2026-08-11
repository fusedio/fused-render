"""GET/PUT /api/prefs — user preferences at ~/.fused-render/prefs.json.

The Preferences page's backend (SPEC §20): a tiny persisted preference store
(shell/storage, beside bookmarks.json/deployments.json) plus the derived,
read-only facts the page shows next to it (engine availability/forcing).

The app's own log is deliberately NOT in this payload: it is disposable
temp-dir output (D68) whose only affordance was a reveal button, and the
desktop tray's "Open app logs" already covers that. Since the call store moved to
~/.fused-render/logs, a second "Logs" heading here read as the call log's
settings rather than as a separate thing.

Four preferences are persisted: **deploy_enabled** (whether the preview-header
Deploy affordance is shown — opt-in, default off; see ``deploy_enabled``),
**reader_enabled** (whether the Reader listen-to-files accessibility mode is
offered — opt-in, default off; see ``reader_enabled``), **default_model** (the
preferred Claude model as a short name, unset by default; see
``default_model``), and the **execution engine** for /api/run:

  * ``"fused"`` (default, D204) — the fused local compute backend (engine.py):
    a folder's ``pyproject.toml`` dependencies resolved into cached venvs,
    ``@fused.udf``/``result`` entrypoints. Effective only while the ``fused``
    package is importable (``fused_engine_available``); otherwise execution
    falls back to builtin and the page says so, which is what keeps a default
    that depends on the environment from being a default that breaks in one.
  * ``"builtin"`` — the built-in executor: fresh subprocess per call, the
    environment that launched the server. Storing it PINS it: D204 flipped the
    default, not the choice, and an importable ``fused`` must not override a
    user who picked builtin — that is precisely D70's surprise.

D204 knowingly re-accepts the install-order-dependent default D70 and D80 both
rejected: the same install can run pages under two contracts depending on what
else shares the environment. The owner's call (2026-08-03) is that the fused
engine's UX has improved enough, and both engines run locally, that the contract
difference is now a much smaller surprise than it was in July.

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

from fused_render.shell import storage

router = APIRouter()

VALID_ENGINES = ("builtin", "fused")
VALID_CALLS_PARAMS = ("full", "keys", "off")
# The default-model preference's value set. SHORT NAMES, not API model ids, and
# `""` — unset — is a first-class member rather than an absence: it is what the
# page's "Automatic" option writes, and it means "let each consumer keep its own
# default" (see default_model). The names are the claude template's own selector
# list (templates/claude/template.html MODELS) — the pref has to speak the same
# vocabulary as the control it presets, and the CLI those names reach accepts
# them as aliases. The relay (server/ai.py) wants a full API id instead, so the
# short→id mapping lives THERE, in one place, next to the caller that needs it.
VALID_DEFAULT_MODELS = ("", "fable", "opus", "sonnet", "haiku")
DEFAULT_CALLS_RETENTION_DAYS = 14


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
    """The persisted engine preference; unset/unknown values read as **fused**.

    D204 flipped this from builtin. It is only the SELECTION —
    ``effective_engine()`` ANDs it with live availability, so "fused by default"
    can never mean "broken by default" on a machine without the package; and a
    stored ``"builtin"`` still pins builtin, because that is a choice the user
    made and not a default to override.
    """
    value = read_prefs().get("engine")
    return value if value in VALID_ENGINES else "fused"


def deploy_enabled() -> bool:
    """Whether the Deploy affordance is shown (default off — opt-in).

    Deploy publishes a page to a public hosted URL through the fused CLI; it's
    an opt-in surface, so the preview-header Deploy button stays hidden until
    the user turns it on from the Preferences page. This gates only the UI
    affordance — it is NOT a security control (the /api/deploy* endpoints keep
    their own X-Fused guard); it just keeps the surface minimal for users who
    don't deploy. Any non-`true` stored value (missing/legacy) reads as off.
    """
    return read_prefs().get("deploy_enabled") is True


def reader_enabled() -> bool:
    """Whether the Reader (listen-to-files) mode is offered (default off — opt-in).

    Reader is an accessibility affordance: a preview mode that reads text files
    and PDFs aloud. It's off until the user turns it on from the Preferences
    page, at which point the reader template's condition.py gate (SPEC CT-12)
    starts allowing the mode on the file types it's bound to. This is a global
    feature switch, not a per-file sniff — the gate ignores the target path and
    consults only this value. Any non-`true` stored value (missing/legacy)
    reads as off.
    """
    return read_prefs().get("reader_enabled") is True


def default_model() -> str:
    """The user's preferred Claude model, as a short name — `""` when unset.

    One preference, two consumers, and neither is allowed to be the authority
    on it: the fused.ai relay (server/ai.py, which maps the short name to the
    full API id it hands the CLI) and the claude chat template (which uses it
    to preselect its model chip). Both rank it the same way — an EXPLICIT
    choice always wins, this is the next-best answer, and each keeps its own
    hardcoded fallback beneath it — so the pref changes what happens when
    nobody asked for a model, never what happens when somebody did.

    An unknown value reads as unset, exactly like `selected_engine`'s: prefs.json
    is a plain file a user may hand-edit, and the one thing that must not happen
    is an arbitrary string reaching a subprocess argv.
    """
    value = read_prefs().get("default_model")
    return value if value in VALID_DEFAULT_MODELS else ""


def calls_enabled() -> bool:
    """Whether the app call log records anything (default ON — see calls.py).

    On by default because a diagnostic that has to be switched on before the
    thing you wanted to diagnose is worthless: the interesting call already
    happened. `FUSED_RENDER_CALLS=0` is the process-level off switch that beats
    this pref. Any non-`false` stored value (including missing) reads as on.
    """
    return read_prefs().get("calls_enabled") is not False


def calls_params_mode() -> str:
    """How much of a run's params the log keeps: full | keys | off.

    Default `full`: params are the inputs the author's own code already
    received and are usually the whole repro (the same named trade-off the
    serve plane's error records make), and locally they are already sitting in
    the URL bar. `keys` keeps only the key names for a page whose param is a
    secret; `off` keeps nothing.
    """
    value = read_prefs().get("calls_params")
    return value if value in VALID_CALLS_PARAMS else "full"


def calls_retention_days() -> int:
    """How long call-log files are kept (default 14, matching the serve plane's
    `errors/` lifecycle rule). 0 disables age-based pruning — the directory
    size cap in calls.sweep() still applies."""
    value = read_prefs().get("calls_retention_days")
    if isinstance(value, int) and 0 <= value <= 3_650:
        return value
    return DEFAULT_CALLS_RETENTION_DAYS


def fused_engine_available() -> bool:
    """Whether the fused backend is importable, resolved off the request path:
    engine.warm() caches it at startup, /api/deploy/install flips it via invalidate()."""
    try:
        from fused_render import engine as _engine
    except ImportError:
        return False
    return _engine.available_nonblocking()


def effective_engine() -> str:
    """The engine /api/run uses **right now** — the single resolver both the
    server's dispatch (`server.current_engine`) and the Preferences page
    (`engine_state`) go through, so the "currently running" label can never
    disagree with what actually executes a page.

    `FUSED_RENDER_ENGINE` overrides the pref (validated at startup by
    `server._forced_engine`, which fails loudly for `=fused` when the package
    is missing); otherwise the persisted pref decides. Availability is
    resolved **live** on every call — an `=auto` override, or a `fused` pref,
    both reflect a mid-session install/removal without a server restart
    (the earlier startup-frozen resolution let the page and dispatch drift
    after an install).
    """
    forced = os.environ.get("FUSED_RENDER_ENGINE")
    if forced is not None:
        requested = forced.strip().lower()
        if requested == "builtin":
            return "builtin"
        # auto / fused: fused iff importable now (=fused was startup-validated).
        return "fused" if fused_engine_available() else "builtin"
    return "fused" if (selected_engine() == "fused" and fused_engine_available()) else "builtin"


def engine_state() -> dict:
    """The engine block of GET /api/prefs.

    ``effective`` is `effective_engine()` — the SAME resolver the server's
    /api/run dispatch uses, so the page never reports a different running
    engine than the one executing pages. ``forced_by`` is the raw
    FUSED_RENDER_ENGINE value when set (the process override that beats the
    pref).
    """
    return {
        "selected": selected_engine(),
        "effective": effective_engine(),
        "forced_by": os.environ.get("FUSED_RENDER_ENGINE"),
        "fused_available": fused_engine_available(),
    }


def _prefs_response() -> dict:
    return {
        "engine": engine_state(),
        # Whether the preview-header Deploy affordance is shown (opt-in).
        "deploy": {"enabled": deploy_enabled()},
        # Whether the Reader (listen-to-files) accessibility mode is offered (opt-in).
        "reader": {"enabled": reader_enabled()},
        # The default Claude model, as a short name; "" = unset (each consumer
        # keeps its own default). `choices` ships the value set with the value
        # so the Preferences page renders the options the server will accept
        # rather than a second copy of this list that can drift from it.
        "model": {"default": default_model(), "choices": list(VALID_DEFAULT_MODELS)},
        # The app call log (calls.py): capture state, param redaction, retention.
        # `dir` lets the page reveal the store through the existing
        # /api/fs/reveal, exactly as `log.path` does.
        "calls": {
            # The STORED prefs — what a PUT round-trips, and what applies once
            # any process override is removed. What is actually in force is the
            # `effective_*` pair below; they differ whenever an env var wins.
            "enabled": calls_enabled(),
            "params": calls_params_mode(),
            "retention_days": calls_retention_days(),
            **_calls_store(),
            **_calls_effective(),
        },
    }


def _calls_store() -> dict:
    """Where the call store is, and whether it is there yet.

    The writer creates the directory on its first append, so between "capture
    on" and "a page actually called something" the path in `dir` does not
    exist. `dir_exists` is reported rather than papered over by creating it
    here: this is a GET, and a read that provisions storage is a side effect in
    the wrong place — the lazy create belongs to the writer (`_append`), which
    is also what keeps an empty store from appearing for someone who never
    records a call. The UI uses the flag to say "nothing recorded yet" instead
    of sending the explorer to a path that will fail to stat.
    """
    # Imported lazily: calls.py imports this module (for the prefs above), so a
    # module-scope import here would be a cycle.
    from fused_render import calls

    path = calls.store_dir()
    return {"dir": path, "dir_exists": os.path.isdir(path)}


def _calls_effective() -> dict:
    """What capture and retention are *actually* doing, and what forced them.

    `FUSED_RENDER_CALLS` and `FUSED_RENDER_CALLS_RETENTION_DAYS` beat the stored
    prefs inside `calls.enabled()`/`calls.retention_days()`, so reporting only
    the stored values lets the page show capture as on while the process has it
    off — the exact failure `engine_state()` exists to prevent, in the same
    payload. Same treatment here: `effective_*` comes from **the resolvers the
    writer itself calls**, never a second copy of the precedence rule, so the
    page cannot report a state the log isn't in; `*_forced_by` carries the raw
    env value so the UI can name what is overriding — and is null unless that
    value is genuinely in force, which is not the same as being set (`_forced_by`).

    Only these two are overridable — the param-redaction mode has no env var, so
    it gets no pair rather than a misleading always-null one.
    """
    # Imported lazily: calls.py imports this module, so a module-scope import
    # here would be a cycle.
    from fused_render import calls

    return {
        "effective_enabled": calls.enabled(),
        "enabled_forced_by": _forced_by(calls.DISABLE_ENV, calls.enabled_override()),
        "effective_retention_days": calls.retention_days(),
        "retention_forced_by": _forced_by(
            calls.RETENTION_DAYS_ENV, calls.retention_days_override()
        ),
    }


def _forced_by(env_name: str, override) -> str | None:
    """The raw value of `env_name` when it is what's actually in force, else None.

    Gated on the writer's override resolver rather than on the variable merely
    being *set*, because for retention those differ: `calls.retention_days()`
    ignores an empty or non-numeric FUSED_RENDER_CALLS_RETENTION_DAYS and keeps
    using the pref. A `forced_by` derived from presence alone then disables the
    UI control and blames the variable for a window it is not setting — leaving
    the user unable to change retention from the page and unable to fix it by
    editing a variable that was never in force (D148). Same shape as the
    `effective_*` values above: ask the writer, don't re-derive.
    """
    return os.environ.get(env_name) if override is not None else None


@router.get("/api/prefs")
def get_prefs():
    return _prefs_response()


@router.put("/api/prefs")
def put_prefs(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    # Partial update: apply only the keys present, so the page can PUT one
    # setting without echoing the others (the engine radio and the deploy
    # toggle are independent controls).
    prefs = read_prefs()
    changed = False
    if "engine" in body:
        engine = body.get("engine")
        if engine not in VALID_ENGINES:
            return JSONResponse(
                {"error": f"'engine' must be one of: {', '.join(VALID_ENGINES)}"}, status_code=400
            )
        prefs["engine"] = engine
        changed = True
    if "deploy_enabled" in body:
        value = body.get("deploy_enabled")
        if not isinstance(value, bool):
            return JSONResponse({"error": "'deploy_enabled' must be a boolean"}, status_code=400)
        prefs["deploy_enabled"] = value
        changed = True
    if "reader_enabled" in body:
        value = body.get("reader_enabled")
        if not isinstance(value, bool):
            return JSONResponse({"error": "'reader_enabled' must be a boolean"}, status_code=400)
        prefs["reader_enabled"] = value
        changed = True
    if "default_model" in body:
        value = body.get("default_model")
        if value not in VALID_DEFAULT_MODELS:
            return JSONResponse(
                {"error": "'default_model' must be one of: "
                          + ", ".join(repr(v) for v in VALID_DEFAULT_MODELS)},
                status_code=400,
            )
        prefs["default_model"] = value
        changed = True
    if "calls_enabled" in body:
        value = body.get("calls_enabled")
        if not isinstance(value, bool):
            return JSONResponse({"error": "'calls_enabled' must be a boolean"}, status_code=400)
        prefs["calls_enabled"] = value
        changed = True
    if "calls_params" in body:
        value = body.get("calls_params")
        if value not in VALID_CALLS_PARAMS:
            return JSONResponse(
                {"error": f"'calls_params' must be one of: {', '.join(VALID_CALLS_PARAMS)}"},
                status_code=400,
            )
        prefs["calls_params"] = value
        changed = True
    if "calls_retention_days" in body:
        value = body.get("calls_retention_days")
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 3_650:
            return JSONResponse(
                {"error": "'calls_retention_days' must be an integer between 0 and 3650"},
                status_code=400,
            )
        prefs["calls_retention_days"] = value
        changed = True
    if not changed:
        return JSONResponse(
            {"error": "no known preference in request (expected 'engine', "
                      "'deploy_enabled', 'reader_enabled', 'default_model', "
                      "'calls_enabled', 'calls_params' and/or "
                      "'calls_retention_days')"},
            status_code=400,
        )
    storage.write_json(_path(), prefs)
    # The call log caches this snapshot for a second to keep prefs.json off its
    # hot path; drop it so a toggle here is visible on the very next call.
    from fused_render import calls as _calls

    _calls.invalidate_prefs_cache()
    # The new state, so the page re-renders from the response (the engine pref
    # is persisted even while FUSED_RENDER_ENGINE forces — it applies once the
    # override is removed; the response's forced_by says so).
    return _prefs_response()
