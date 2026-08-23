"""A Python client for the local fused-render AI API (SPEC PY-19, D448-D450).

Mirrors `fused.ai` from `fused_render/static/runtime.js` — same names, same
option names, same closed-envelope rejection (D413) — for Python code that
wants local inference without re-implementing the model layer. See
`fused.ai.text/stream/transcribe/image/embed/models/cancel` there for the
JS-side contract this restates.

**Travels with `appenv.py`.** This module is stdlib only (`json`, `os`,
`socket`, `time`, `urllib`) and MUST NOT `import fused_render` — a `.py` data
file runs in a subprocess with `PYTHONPATH` stripped and cannot see the
package (SPEC PY-15; `_child.py` has a dedicated diagnostic for exactly this
mistake). It imports its sibling `appenv.py` instead, which already knows the
env-var contract for the shell's home dir — re-deriving that here would be
the second copy that drifts. Vendoring this file for use outside a
fused-render page (SPEC PY-19's bootstrap, or a standalone copy) means
vendoring `appenv.py` beside it; neither is useful alone.

**Three ways to reach this module** (see `docs/PYTHON_CLIENT_DESIGN.md`):
a user `.py` under a running server gets `import fused_ai` for free — the
engine seeds its path onto `sys.path` (`_child.py` and `engine.py`'s
generated wrapper, in lockstep); an external process reads `server.json`
(written by `server.export_app_env`'s neighbours) and `sys.path.insert`s its
`shared` value before importing; an app wanting zero coupling vendors this
file plus `appenv.py`. There is no `pip install`-able package form — that
would drag fastapi/duckdb/pyarrow into a caller that wants a 300-line HTTP
shim.

**Blocking by default.** `transcribe`, `image`, and `models.load`/`download`
POST to a job-backed endpoint that answers immediately with a `jobId`, the
way a page's `await fused.ai.image(...)` does — but a Python caller has a
thread to spend, so by default this module polls `GET /api/jobs` for that id
until it reaches a terminal state and returns the settled result. Pass
`wait=False` for the immediate reply (`{"jobId": ..., "path": ..., ...}`) to
drive the loop yourself, and `on_progress=` to observe each polled row. That
is the one place this surface deliberately differs from the JS one: `await`
becomes `return`.

**The request envelope is closed (D413) and this module does not re-check
it.** An option the server does not recognise comes back as a 400 from the
server itself — keeping a third copy of the whitelist here (beside
`runtime.js`'s and the server's own `_IMAGE_OPTIONS`/`_TRANSCRIBE_OPTIONS`)
is exactly how the three would drift. The `_IMAGE_WIRE_KEYS`/
`_TRANSCRIBE_WIRE_KEYS` sets below exist ONLY so a test can pin them against
the server's own constants — they name what this module forwards, not a
gate it enforces.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse


def _load_sibling_appenv():
    """Load THIS file's own `appenv.py`, by path — not `import appenv`.

    The shared dir is APPENDED to a user module's `sys.path` (`_child.py`/
    `engine.py`'s wrapper), deliberately, so a user's own same-named module
    still wins for the module a user script would `import` by name. That is
    exactly wrong for `fused_ai.py`'s OWN dependency on `appenv.py`: a user
    who happens to have their own `appenv.py` beside their script would
    otherwise shadow the shipped one the instant `sys.path` puts the user's
    module dir first, and this module would break calling `appenv.origin()`
    against whatever the user's stand-in does or does not define — the
    `AttributeError` a user should never see from a dependency they don't
    know exists. Loading by the file's own location sidesteps `sys.path`
    order entirely, so this module always gets ITS sibling regardless of
    what else is importable under the name `appenv`.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "appenv.py")
    spec = importlib.util.spec_from_file_location("_fused_ai_appenv", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


appenv = _load_sibling_appenv()

# Connect-probe timeout for a `server.json` origin: short, because a live
# server answers a TCP handshake in microseconds and a dead one should not
# make every call wait — see `resolve_origin`.
_PROBE_TIMEOUT_S = 0.35
# Default network timeout for a request. Generation and job polling can run
# for minutes, but that is `timeout=` on the caller's own wait loop, not this
# socket-level bound — this is "the server stopped answering the socket",
# which should surface much sooner.
_DEFAULT_TIMEOUT_S = 120.0
# How often the job-wait loop polls `GET /api/jobs`. Comfortably above the
# job registry's own reporting cadence (`jobs.py` names 1.5-2s as typical),
# so an ordinary job never looks slow because of this module's own polling.
_JOB_POLL_INTERVAL_S = 0.7

# The basename `server.export_app_env`'s neighbour writes under the shell
# home dir (`appenv.home_dir()` — already branch-resolved). Named here too so
# a test can point at the same fact without restating the join.
SERVER_JSON_NAME = "server.json"

# What this module forwards per call, restated here ONLY so a test can pin it
# against the server's own `_IMAGE_OPTIONS`/`_TRANSCRIBE_OPTIONS`
# (`routers/ai_runtime.py`) — the same drift guard that already stops
# `runtime.js` and the server from disagreeing. Never consulted to reject a
# caller's option: the server is the one closed envelope (D413).
_IMAGE_WIRE_KEYS = frozenset(
    {"prompt", "model", "width", "height", "steps", "guidance", "seed", "image"})
_TRANSCRIBE_WIRE_KEYS = frozenset(
    {"path", "model", "language", "task", "initialPrompt", "vad", "diarize",
     "speakers", "words"})


class ServerNotRunning(Exception):
    """No reachable fused-render server for this process.

    Distinct from `AiError` on purpose (see the module docstring's design
    note): "the app isn't running" and "the call failed" want different
    responses from a caller, and folding them into one exception type would
    make every catch site re-derive which is which from the message text.
    """


class AiError(Exception):
    """One failed AI call, off the house `{ok, error:{type, message}}` wire
    shape (or the plainer `{"error": "..."}` a job-backed endpoint's
    validation returns — `_error_from_payload` maps both onto this one type).

    `job_id` is set only for the one wire case that carries one: an embed
    call against a cold model answers 409 with the load it just started
    (`/api/ai/embed`'s `_embed_error`), and a caller wants that id to watch
    the same way `fused.ai`'s own JS reader does.
    """

    def __init__(self, type_: str, message: str, status: int | None = None,
                 job_id: str | None = None):
        super().__init__(message)
        self.type = type_
        self.message = message
        self.status = status
        self.job_id = job_id

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (f"AiError(type={self.type!r}, message={self.message!r}, "
                f"status={self.status!r})")


# ------------------------------------------------------------- origin lookup


def _server_json_path() -> str:
    return os.path.join(appenv.home_dir(), SERVER_JSON_NAME)


def _probe(origin: str, timeout: float = _PROBE_TIMEOUT_S) -> bool:
    """True iff a plain TCP connect to `origin`'s host:port succeeds.

    Not an HTTP request — a dead process leaves nothing listening at all, and
    a bare connect is the cheapest way to tell "the server that wrote this
    file is gone" from "it is merely slow to answer this particular route".
    """
    parsed = urlparse(origin)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port
    if not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_origin() -> str:
    """The origin (`http://host:port`) to call, resolved in order:

    1. `FUSED_RENDER_ORIGIN` (via `appenv.origin()`) — set for every process
       this server spawned, so a `.py` data file never has to look further.
    2. `server.json` under the shell home dir, written by the server at bind
       time (`server.export_app_env`'s neighbour) for a process the server
       did NOT spawn (a user-launched app). Connect-probed before use: the
       file can outlive a crashed server (staleness is this module's problem,
       not a heartbeat's — the server writes no liveness beyond the file
       itself), and a stale port must fall through rather than be trusted.
    3. Neither -> `ServerNotRunning`. No `branch_port()` guess: this module
       cannot re-derive branch-ref resolution without importing
       `fused_render`, and a guessed port that happens to be free but wrong
       is a worse failure than a clear "nothing is running".
    """
    origin = appenv.origin()
    if origin:
        return origin

    path = _server_json_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        raise ServerNotRunning(
            "no fused-render server is running: FUSED_RENDER_ORIGIN is unset "
            f"and {path} could not be read"
        ) from None

    origin = data.get("origin") if isinstance(data, dict) else None
    if not isinstance(origin, str) or not origin or not _probe(origin):
        raise ServerNotRunning(
            f"{path} names {origin!r}, but nothing answered there — the "
            "server that wrote it is not running any more"
        )
    return origin


# ------------------------------------------------------------------ transport


def _error_from_payload(status: int, payload: object) -> AiError:
    """Map an error response body onto `AiError`.

    Two wire shapes reach here. `/api/ai` and `/api/ai/embed` answer
    `{"ok": false, "error": {"type", "message"}}` — read verbatim. Everything
    job-backed (`/api/ai/image`, `/api/ai/transcribe`, `/api/ai/runtime/*`,
    `/api/jobs/*`) answers the plainer `server/common.py::_error` shape,
    `{"error": "a message string"}`, with no `type` at all — the same
    fallback `runtime.js`'s own `aiPost` uses: a 409 there is a fact about
    this machine ("unavailable" — model still loading, or refused), anything
    else is a malformed request.
    """
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return AiError(
                err.get("type") or "error", err.get("message") or "",
                status, job_id=err.get("jobId"))
        if isinstance(err, str) and err:
            type_ = "unavailable" if status == 409 else "bad_request"
            return AiError(type_, err, status)
    return AiError("error", f"HTTP {status}", status)


def _read_json_body(raw: bytes) -> object:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _request(method: str, path: str, body: dict | None = None,
             timeout: float = _DEFAULT_TIMEOUT_S):
    """One HTTP round trip against the resolved origin. Returns the open
    response (caller reads it) or raises `ServerNotRunning`/`AiError`.

    `X-Fused: 1` rides on every POST — not authentication (D3), it forces a
    CORS preflight a foreign page cannot pass, and a local Python process
    just sends it so no caller here has to know the rule exists.
    """
    origin = resolve_origin()
    url = origin.rstrip("/") + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if method == "POST":
        headers["X-Fused"] = "1"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        payload = _read_json_body(e.read())
        raise _error_from_payload(e.code, payload) from None
    except urllib.error.URLError as e:
        raise AiError("network_error", str(e.reason)) from None


def _get_json(path: str, timeout: float = _DEFAULT_TIMEOUT_S) -> dict:
    resp = _request("GET", path, timeout=timeout)
    payload = _read_json_body(resp.read())
    return payload if isinstance(payload, dict) else {}


def _post_json(path: str, body: dict, timeout: float = _DEFAULT_TIMEOUT_S) -> dict:
    resp = _request("POST", path, body=body, timeout=timeout)
    payload = _read_json_body(resp.read())
    return payload if isinstance(payload, dict) else {}


def _parse_ndjson(chunks):
    """Yield one parsed JSON object per NDJSON line out of an iterable of
    raw byte chunks, buffering across chunk boundaries.

    A chunk is whatever one `read()` off the socket happened to return, so a
    line can be split anywhere — including mid-line across two chunks, or
    several lines landing in one chunk. Both are handled by buffering
    everything and only ever cutting on `\\n`.
    """
    buffer = b""
    for chunk in chunks:
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if line.strip():
                yield json.loads(line.decode("utf-8"))
    if buffer.strip():
        yield json.loads(buffer.decode("utf-8"))


# ---------------------------------------------------------------- job waiting


#: `_wait_job`'s tolerance for a missing row, once one has been seen at least
#: once — matching `runtime.js`'s `watchJob`, which resolves null only after
#: 5 CONSECUTIVE misses. Not paranoia: `jobs.py::_sweep`'s own comments say a
#: finished SERVER row is evicted on the very next `list_jobs()` once the
#: registry is over `MAX_JOBS` (64) — the designed transcription-queue case,
#: SPEC AI-10a — so a single missed poll right as a big batch finishes is
#: ordinary, not a sign the job vanished.
_JOB_MISS_TOLERANCE = 5

#: How long a `stalled` row (`jobs.py::is_stalled`, which flips at
#: `STALE_AFTER_S` == 30s of reporter silence) must stay stalled before this
#: module gives up on it. `is_stalled` only means "no update in 30s" — a
#: phase that legitimately ticks less often (a slow denoise step, a worker
#: between reports) trips it while the work continues, and the registry
#: itself keeps a running-but-stalled row alive for ten minutes
#: (`STALE_DROP_S`) precisely because it is a label, not a verdict. Not
#: reading `jobs.STALE_AFTER_S` directly — this module must not import
#: `fused_render` (SPEC PY-15) — so the grace period is restated here as an
#: independent constant, comfortably inside `STALE_DROP_S`.
_JOB_STALL_GRACE_S = 60.0


def _wait_job(job_id: str, on_progress=None, timeout: float | None = None,
             poll_interval: float = _JOB_POLL_INTERVAL_S) -> dict:
    """Poll `GET /api/jobs` for `job_id` until it reaches a terminal state
    (`jobs.py`'s `TERMINAL_STATES`: done/error/cancelled) and return that row.

    A row that has gone missing, or that reports `stalled`, is not treated as
    fatal on the FIRST observation — see `_JOB_MISS_TOLERANCE` and
    `_JOB_STALL_GRACE_S` above for why each needs to persist first.
    """
    started = time.monotonic()
    seen = False
    misses = 0
    stalled_since: float | None = None
    while True:
        if timeout is not None and (time.monotonic() - started) > timeout:
            raise AiError("timeout",
                          f"job {job_id!r} did not finish within {timeout}s")
        payload = _get_json("/api/jobs")
        jobs = payload.get("jobs") or []
        record = next((j for j in jobs if j.get("id") == job_id), None)
        if record is None:
            if seen:
                misses += 1
                if misses >= _JOB_MISS_TOLERANCE:
                    raise AiError(
                        "error", f"job {job_id!r} is no longer being reported")
            time.sleep(poll_interval)
            continue
        seen = True
        misses = 0
        if callable(on_progress):
            on_progress(record)
        if record.get("stalled"):
            now = time.monotonic()
            if stalled_since is None:
                stalled_since = now
            elif (now - stalled_since) > _JOB_STALL_GRACE_S:
                raise AiError("stalled",
                              f"job {job_id!r} stopped reporting progress")
        else:
            stalled_since = None
        if record.get("state") != "running":
            return record
        time.sleep(poll_interval)


def _raise_for_terminal_job(job: dict) -> None:
    state = job.get("state")
    if state == "done":
        return
    if state == "cancelled":
        raise AiError("cancelled", job.get("message") or "the job was cancelled")
    raise AiError("ai_error", job.get("message") or "the job failed")


# --------------------------------------------------------------------- text


def text(prompt: str, model: str | None = None, effort: str | None = None,
         system_prompt: str | None = None, timeout: float = _DEFAULT_TIMEOUT_S) -> str:
    """`POST /api/ai` and return the completion text. Mirrors `fused.ai()`
    without `onChunk` — see `stream()` for the streaming form."""
    body: dict = {"prompt": prompt}
    if model is not None:
        body["model"] = model
    if effort is not None:
        body["effort"] = effort
    if system_prompt is not None:
        body["system_prompt"] = system_prompt
    payload = _post_json("/api/ai", body, timeout=timeout)
    if not payload.get("ok"):
        raise _error_from_payload(200, payload)
    result = payload.get("result") or {}
    return result.get("text") or ""


def stream(prompt: str, model: str | None = None, effort: str | None = None,
           system_prompt: str | None = None, timeout: float = _DEFAULT_TIMEOUT_S):
    """`POST /api/ai` with `{"stream": true}` and yield text chunks as they
    arrive, mirroring `fused.ai(prompt, {onChunk})`.

    The NDJSON body is `{"type":"chunk","text":...}` lines closed by one
    `{"type":"done", ...}` line. A `done` frame with `ok: false` is an error
    that arrived AFTER the first byte left the wire — the server demotes it
    from a status code to a frame (`_ai_relay`'s own docstring) — so this
    raises `AiError` from inside the generator rather than returning it.
    """
    body = {"prompt": prompt, "stream": True}
    if model is not None:
        body["model"] = model
    if effort is not None:
        body["effort"] = effort
    if system_prompt is not None:
        body["system_prompt"] = system_prompt
    resp = _request("POST", "/api/ai", body=body, timeout=timeout)

    def _chunks():
        while True:
            piece = resp.read(4096)
            if not piece:
                return
            yield piece

    for frame in _parse_ndjson(_chunks()):
        ftype = frame.get("type")
        if ftype == "chunk":
            yield frame.get("text") or ""
        elif ftype == "done":
            if not frame.get("ok"):
                err = frame.get("error") or {}
                raise AiError(err.get("type") or "ai_error",
                              err.get("message") or "", job_id=err.get("jobId"))
            return


# --------------------------------------------------------------- transcribe


def transcribe(path: str, model: str | None = None, language: str | None = None,
               task: str | None = None, initial_prompt: str | None = None,
               vad: bool | None = None, diarize: bool | None = None,
               speakers: int | None = None, words: bool | None = None,
               wait: bool = True, on_progress=None,
               timeout: float | None = None) -> dict:
    """`POST /api/ai/transcribe`. Job-backed (SPEC AI-9), so this blocks by
    default: it posts, waits for the job to finish, and returns the settled
    reply — `wait=False` returns the immediate `{"jobId", "path", "output",
    ...}` reply instead, for a caller that wants to drive its own loop.

    `path` is resolved to an absolute path locally rather than sent relative
    with a `base` — `/api/ai/transcribe` only accepts a relative `path`
    alongside an absolute `base` naming the calling PAGE (RH-1), and
    `_child.py` already chdirs a running `.py` to its own directory, so a
    relative path here already means "beside this file" without one.
    """
    resolved = os.path.abspath(os.path.expanduser(path))
    body: dict = {"path": resolved}
    for key, value in (
        ("model", model), ("language", language), ("task", task),
        ("initialPrompt", initial_prompt), ("vad", vad), ("diarize", diarize),
        ("speakers", speakers), ("words", words),
    ):
        if value is not None:
            body[key] = value
    reply = _post_json("/api/ai/transcribe", body)
    if not wait:
        return reply
    job = _wait_job(_require_job_id(reply, "/api/ai/transcribe"),
                    on_progress=on_progress, timeout=timeout)
    _raise_for_terminal_job(job)
    return reply


# ------------------------------------------------------------------- image


def image(prompt: str, model: str | None = None, width: int | None = None,
          height: int | None = None, steps: int | None = None,
          guidance: float | None = None, seed: int | None = None,
          image: str | None = None, wait: bool = True, on_progress=None,
          timeout: float | None = None) -> dict:
    """`POST /api/ai/image`. Job-backed like `transcribe()`, same default.

    `image` (a base image to edit) follows the identical local-abspath rule
    `path` does above, for the same reason (RH-1) — the server's `base`
    option is for a page's own `?path=`, which this module has none of.
    """
    body: dict = {"prompt": prompt}
    for key, value in (
        ("model", model), ("width", width), ("height", height),
        ("steps", steps), ("guidance", guidance), ("seed", seed),
    ):
        if value is not None:
            body[key] = value
    if image is not None:
        body["image"] = os.path.abspath(os.path.expanduser(image))
    reply = _post_json("/api/ai/image", body)
    if not wait:
        return reply
    job = _wait_job(_require_job_id(reply, "/api/ai/image"),
                    on_progress=on_progress, timeout=timeout)
    _raise_for_terminal_job(job)
    return reply


# ------------------------------------------------------------------- embed


def embed(texts: list | None = None, paths: list | None = None,
          model: str | None = None, timeout: float = _DEFAULT_TIMEOUT_S) -> dict:
    """`POST /api/ai/embed`. Not job-backed (`/api/ai/embed`'s own docstring:
    one forward pass over a short batch, over before a progress row would
    ever draw) — the reply IS the result, exactly one of `texts`/`paths`."""
    if (texts is None) == (paths is None):
        raise AiError("bad_request", "pass exactly one of 'texts' or 'paths'")
    if paths is not None:
        body: dict = {"paths": [os.path.abspath(os.path.expanduser(p)) for p in paths]}
    else:
        body = {"texts": list(texts)}
    if model is not None:
        body["model"] = model
    payload = _post_json("/api/ai/embed", body, timeout=timeout)
    if not payload.get("ok"):
        raise _error_from_payload(200, payload)
    return payload.get("result") or {}


# ------------------------------------------------------------------- models


#: `supervisor.py`'s OWN worker-state vocabulary (`Worker.state`) — NOT
#: `jobs.py`'s registry states. `POST /api/ai/runtime/load`'s reply carries
#: one of these (`_start_resident`), and only these four mean "still on its
#: way up". The "joining an in-flight bring-up" branch answers `state:
#: "ready"` (or, in principle, a stale "error") with NO accompanying
#: `_report()` call when the model was already resident — so the `sys:`
#: job row may be long swept (`jobs.FINISHED_TTL_S` == 30s) by the time a
#: caller asks a second time, and polling for it at all is the bug: the
#: reply's own state already answers the question.
_MODEL_LOADING_STATES = frozenset({"starting", "venv", "downloading", "loading"})


def _require_job_id(reply: dict, endpoint: str) -> str:
    """`reply["jobId"]`, or `AiError` instead of a bare `KeyError` — a 200
    whose body doesn't match the shape this module expects (an unexpected
    server change, a proxy mangling the response) must still surface through
    the one exception type callers are told to catch."""
    job_id = reply.get("jobId")
    if not isinstance(job_id, str) or not job_id:
        raise AiError("bad_response", f"{endpoint} replied with no 'jobId'")
    return job_id


class _Models:
    """`fused.ai.models` mirrored: list/catalog are plain reads;
    load/download are job-backed (block by default, like `transcribe`);
    unload is immediate."""

    @staticmethod
    def list() -> dict:
        return _get_json("/api/ai/runtime")

    @staticmethod
    def catalog() -> dict:
        return _get_json("/api/ai/catalog")

    @staticmethod
    def load(model_id: str, capability: str | None = None, wait: bool = True,
             on_progress=None, timeout: float | None = None) -> dict:
        body = {"model": model_id}
        if capability is not None:
            body["capability"] = capability
        reply = _post_json("/api/ai/runtime/load", body)
        if not wait or reply.get("state") not in _MODEL_LOADING_STATES:
            return reply
        job = _wait_job(_require_job_id(reply, "/api/ai/runtime/load"),
                        on_progress=on_progress, timeout=timeout)
        _raise_for_terminal_job(job)
        return reply

    @staticmethod
    def download(model_id: str, capability: str | None = None, wait: bool = True,
                on_progress=None, timeout: float | None = None) -> dict:
        body = {"model": model_id}
        if capability is not None:
            body["capability"] = capability
        reply = _post_json("/api/ai/runtime/download", body)
        if not wait or reply.get("state") not in _MODEL_LOADING_STATES:
            return reply
        job = _wait_job(_require_job_id(reply, "/api/ai/runtime/download"),
                        on_progress=on_progress, timeout=timeout)
        _raise_for_terminal_job(job)
        return reply

    @staticmethod
    def unload(model_id: str | None = None, capability: str | None = None) -> dict:
        body = {"model": model_id} if model_id is not None else {"capability": capability}
        return _post_json("/api/ai/runtime/unload", body)


models = _Models()


def cancel(capability: str | None = None) -> bool:
    """`POST /api/ai/cancel`. Stops generation in flight on a resident model,
    keeping it loaded. False when there was nothing to stop — not an error."""
    body = {"capability": capability} if capability else {}
    payload = _post_json("/api/ai/cancel", body)
    return bool(payload.get("cancelled"))


class _Ai:
    """`from fused_ai import ai; ai.text(...)` — the 1:1 mirror of
    `fused.ai` from `runtime.js`."""

    text = staticmethod(text)
    stream = staticmethod(stream)
    transcribe = staticmethod(transcribe)
    image = staticmethod(image)
    embed = staticmethod(embed)
    models = models
    cancel = staticmethod(cancel)


ai = _Ai()
