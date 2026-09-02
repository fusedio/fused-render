"""Tests for `fused_render/templates/shared/fused_ai.py` — the stdlib-only
Python client for `fused.ai` (SPEC PY-19, D470-D472).

Loaded the way production loads it: the shared dir goes on `sys.path` (what
both engines' path-seeding does) and then `import fused_ai` — not exec'd
standalone, because this module `import appenv`s its sibling and that import
has to resolve the same way it would for a real caller.

The HTTP layer is mocked throughout (`urllib.request.urlopen`, monkeypatched)
— no real socket, no real model, no dev server. `test_engine.py` and
`test_builtin_executor_project_env.py`-style tests cover the two-engine path
seeding that makes `import fused_ai` reachable in the first place; this file
is only the client's own logic.
"""
import io
import json
import os
import sys
import urllib.error

import pytest

_SHARED_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "shared")
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

import appenv  # noqa: E402 - path seeded above, matching production
import fused_ai  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Never read a real `~/.fused-render/server.json` from the dev machine."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FUSED_RENDER_HOME_DIR", raising=False)
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)


def _write_server_json(path, **fields):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fields, f)


class _FakeHTTPResponse:
    """Enough of `http.client.HTTPResponse` for `_request`'s callers: a
    `.read()` (whole-body, for `_get_json`/`_post_json`) and repeated
    `.read(n)` (chunked, for `stream()`'s NDJSON reader)."""

    def __init__(self, body: bytes, chunks: list[bytes] | None = None):
        self._body = body
        self._chunks = list(chunks) if chunks is not None else None
        self.closed = False

    def read(self, n=None):
        if n is None:
            return self._body
        if self._chunks is None:
            return b""
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self):
        self.closed = True


class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        super().__init__("http://x", code, "err", {}, io.BytesIO(body))
        self._body = body

    def read(self):
        return self._body


# ------------------------------------------------------------- origin lookup


def test_env_origin_wins_over_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:9999")
    _write_server_json(fused_ai._server_json_path(), origin="http://127.0.0.1:1234")
    assert fused_ai.resolve_origin() == "http://127.0.0.1:9999"


def test_a_missing_file_and_no_env_raises_server_not_running():
    with pytest.raises(fused_ai.ServerNotRunning):
        fused_ai.resolve_origin()


def test_a_stale_file_whose_port_refuses_a_connect_falls_through(monkeypatch):
    _write_server_json(fused_ai._server_json_path(), origin="http://127.0.0.1:1")
    monkeypatch.setattr(fused_ai, "_probe", lambda origin, timeout=0.35: False)
    with pytest.raises(fused_ai.ServerNotRunning):
        fused_ai.resolve_origin()


def test_a_live_file_origin_is_used_when_probe_succeeds(monkeypatch):
    _write_server_json(fused_ai._server_json_path(), origin="http://127.0.0.1:4242")
    monkeypatch.setattr(fused_ai, "_probe", lambda origin, timeout=0.35: True)
    assert fused_ai.resolve_origin() == "http://127.0.0.1:4242"


def test_server_json_path_is_under_appenv_home_dir():
    assert fused_ai._server_json_path() == os.path.join(
        appenv.home_dir(), "server.json")


# ---------------------------------------------------------------- AiError


def test_ok_false_dict_error_shape_maps_to_aierror():
    err = fused_ai._error_from_payload(
        502, {"ok": False, "error": {"type": "ai_error", "message": "boom"}})
    assert isinstance(err, fused_ai.AiError)
    assert err.type == "ai_error"
    assert err.message == "boom"
    assert err.status == 502


def test_ok_false_error_with_job_id_is_carried_through():
    err = fused_ai._error_from_payload(
        409, {"ok": False, "error": {"type": "model_loading", "message": "loading",
                                     "jobId": "sys:ai-load:abc"}})
    assert err.job_id == "sys:ai-load:abc"


def test_plain_error_string_shape_maps_to_bad_request_or_unavailable():
    bad = fused_ai._error_from_payload(400, {"error": "nope"})
    assert bad.type == "bad_request"
    unavailable = fused_ai._error_from_payload(409, {"error": "loading"})
    assert unavailable.type == "unavailable"


def test_text_raises_aierror_from_http_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise _FakeHTTPError(502, {"ok": False,
                                   "error": {"type": "ai_error", "message": "bad"}})

    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:1")
    monkeypatch.setattr(fused_ai.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(fused_ai.AiError) as exc:
        fused_ai.text("hi")
    assert exc.value.type == "ai_error"
    assert exc.value.message == "bad"


def test_text_returns_result_text_on_success(monkeypatch):
    payload = {"ok": True, "result": {"text": "hi there", "model": "opus", "usage": None}}

    def fake_urlopen(req, timeout=None):
        assert req.get_header("X-fused") == "1"
        assert json.loads(req.data) == {"prompt": "hi"}
        return _FakeHTTPResponse(json.dumps(payload).encode())

    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:1")
    monkeypatch.setattr(fused_ai.urllib.request, "urlopen", fake_urlopen)
    assert fused_ai.text("hi") == "hi there"


# -------------------------------------------------------------- NDJSON stream


def test_parse_ndjson_handles_a_chunk_split_mid_line():
    whole = json.dumps({"type": "chunk", "text": "hello"}) + "\n" + \
        json.dumps({"type": "done", "ok": True, "result": {}}) + "\n"
    raw = whole.encode("utf-8")
    # Split at an arbitrary byte offset that lands inside the first line.
    cut = 5
    chunks = [raw[:cut], raw[cut:cut + 3], raw[cut + 3:]]
    frames = list(fused_ai._parse_ndjson(chunks))
    assert frames == [
        {"type": "chunk", "text": "hello"},
        {"type": "done", "ok": True, "result": {}},
    ]


def test_parse_ndjson_raises_aierror_not_a_bare_json_error_on_a_truncated_line():
    """A connection that dies mid-line must surface through the exception
    contract callers are told to catch (AiError/ServerNotRunning), not as a
    bare json.JSONDecodeError."""
    chunks = [b'{"type": "chunk", "tex']  # truncated, never closed
    with pytest.raises(fused_ai.AiError) as exc:
        list(fused_ai._parse_ndjson(chunks))
    assert exc.value.type == "bad_response"


def test_get_json_raises_aierror_not_a_bare_oserror_on_a_read_failure(monkeypatch):
    class _DyingResponse:
        def read(self, n=None):
            raise TimeoutError("timed out")

    monkeypatch.setattr(fused_ai, "_request", lambda *a, **kw: _DyingResponse())
    with pytest.raises(fused_ai.AiError) as exc:
        fused_ai._get_json("/api/jobs")
    assert exc.value.type == "network_error"


def test_post_json_raises_aierror_not_a_bare_oserror_on_a_read_failure(monkeypatch):
    class _DyingResponse:
        def read(self, n=None):
            raise OSError("connection reset")

    monkeypatch.setattr(fused_ai, "_request", lambda *a, **kw: _DyingResponse())
    with pytest.raises(fused_ai.AiError) as exc:
        fused_ai._post_json("/api/ai", {"prompt": "hi"})
    assert exc.value.type == "network_error"


def test_stream_yields_chunks_and_stops_on_done(monkeypatch):
    lines = [
        json.dumps({"type": "chunk", "text": "a"}),
        json.dumps({"type": "chunk", "text": "b"}),
        json.dumps({"type": "done", "ok": True, "result": {"text": "ab"}}),
    ]
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    resp = _FakeHTTPResponse(b"", chunks=[raw[:10], raw[10:20], raw[20:]])

    def fake_urlopen(req, timeout=None):
        assert json.loads(req.data)["stream"] is True
        return resp

    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:1")
    monkeypatch.setattr(fused_ai.urllib.request, "urlopen", fake_urlopen)
    got = list(fused_ai.stream("hi"))
    assert got == ["a", "b"]


def test_stream_closes_the_response_on_a_normal_done_frame(monkeypatch):
    lines = [
        json.dumps({"type": "chunk", "text": "a"}),
        json.dumps({"type": "done", "ok": True, "result": {"text": "a"}}),
    ]
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    resp = _FakeHTTPResponse(b"", chunks=[raw])
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:1")
    monkeypatch.setattr(fused_ai.urllib.request, "urlopen",
                       lambda req, timeout=None: resp)
    list(fused_ai.stream("hi"))
    assert resp.closed is True


def test_stream_closes_the_response_on_an_ok_false_done_frame(monkeypatch):
    lines = [
        json.dumps({"type": "done", "ok": False,
                   "error": {"type": "ai_error", "message": "boom"}}),
    ]
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    resp = _FakeHTTPResponse(b"", chunks=[raw])
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:1")
    monkeypatch.setattr(fused_ai.urllib.request, "urlopen",
                       lambda req, timeout=None: resp)
    gen = fused_ai.stream("hi")
    with pytest.raises(fused_ai.AiError):
        next(gen)
    assert resp.closed is True


def test_stream_closes_the_response_when_the_caller_abandons_it_early(monkeypatch):
    """Not closing here is worse than a leaked socket: the server's own
    `finally` (server/ai.py's _ai_relay) only cancels the in-flight
    generation once the client actually disconnects — an open-but-unread
    response keeps burning tokens server-side for a stream nobody reads."""
    lines = [
        json.dumps({"type": "chunk", "text": "a"}),
        json.dumps({"type": "chunk", "text": "b"}),
        json.dumps({"type": "done", "ok": True, "result": {"text": "ab"}}),
    ]
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    resp = _FakeHTTPResponse(b"", chunks=[raw])
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:1")
    monkeypatch.setattr(fused_ai.urllib.request, "urlopen",
                       lambda req, timeout=None: resp)
    gen = fused_ai.stream("hi")
    assert next(gen) == "a"
    gen.close()  # the caller broke out of a `for c in stream(...): break`
    assert resp.closed is True


def test_stream_raises_on_an_ok_false_done_frame(monkeypatch):
    lines = [
        json.dumps({"type": "chunk", "text": "a"}),
        json.dumps({"type": "done", "ok": False,
                   "error": {"type": "ai_error", "message": "died mid-stream"}}),
    ]
    raw = ("\n".join(lines) + "\n").encode("utf-8")

    def fake_urlopen(req, timeout=None):
        return _FakeHTTPResponse(b"", chunks=[raw])

    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:1")
    monkeypatch.setattr(fused_ai.urllib.request, "urlopen", fake_urlopen)
    gen = fused_ai.stream("hi")
    assert next(gen) == "a"
    with pytest.raises(fused_ai.AiError) as exc:
        next(gen)
    assert exc.value.type == "ai_error"
    assert exc.value.message == "died mid-stream"


def test_stream_raises_aierror_not_a_bare_error_on_a_socket_failure_mid_read(monkeypatch):
    class _DyingResponse:
        def read(self, n=None):
            raise TimeoutError("timed out mid-stream")

        def close(self):
            pass

    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:1")
    monkeypatch.setattr(fused_ai.urllib.request, "urlopen",
                       lambda req, timeout=None: _DyingResponse())
    gen = fused_ai.stream("hi")
    with pytest.raises(fused_ai.AiError) as exc:
        next(gen)
    assert exc.value.type == "network_error"


# ----------------------------------------------------------------- job wait


def _jobs_payload(*records):
    return {"jobs": list(records), "now": 0}


def test_wait_job_returns_on_terminal_state(monkeypatch):
    calls = []

    def fake_get_json(path, timeout=None):
        calls.append(path)
        return _jobs_payload({"id": "sys:x", "state": "done", "stalled": False})

    monkeypatch.setattr(fused_ai, "_get_json", fake_get_json)
    monkeypatch.setattr(fused_ai.time, "sleep", lambda s: None)
    record = fused_ai._wait_job("sys:x")
    assert record["state"] == "done"


def test_wait_job_raises_on_a_persisted_stall(monkeypatch):
    """Superseded by the review fix: a stall must PERSIST past
    `_JOB_STALL_GRACE_S` before this raises (see
    `test_wait_job_does_not_raise_on_a_single_stalled_tick` and
    `test_wait_job_raises_once_stalled_persists_past_the_grace_period` for
    the two halves) — a fake clock is required here too, or this busy-loops
    real wall-clock seconds waiting out the grace period."""
    ticks = iter([0.0, 0.0, 200.0])

    def fake_monotonic():
        return next(ticks, 200.0)

    def fake_get_json(path, timeout=None):
        return _jobs_payload({"id": "sys:x", "state": "running", "stalled": True})

    monkeypatch.setattr(fused_ai.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(fused_ai, "_get_json", fake_get_json)
    monkeypatch.setattr(fused_ai.time, "sleep", lambda s: None)
    with pytest.raises(fused_ai.AiError) as exc:
        fused_ai._wait_job("sys:x")
    assert exc.value.type == "stalled"


def test_wait_job_raises_on_timeout(monkeypatch):
    ticks = iter([0.0, 0.0, 100.0])

    def fake_monotonic():
        return next(ticks, 100.0)

    def fake_get_json(path, timeout=None):
        return _jobs_payload({"id": "sys:x", "state": "running", "stalled": False})

    monkeypatch.setattr(fused_ai.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(fused_ai, "_get_json", fake_get_json)
    monkeypatch.setattr(fused_ai.time, "sleep", lambda s: None)
    with pytest.raises(fused_ai.AiError) as exc:
        fused_ai._wait_job("sys:x", timeout=1.0)
    assert exc.value.type == "timeout"


def test_wait_job_calls_on_progress(monkeypatch):
    seen = []
    states = iter(["running", "done"])

    def fake_get_json(path, timeout=None):
        return _jobs_payload({"id": "sys:x", "state": next(states), "stalled": False})

    monkeypatch.setattr(fused_ai, "_get_json", fake_get_json)
    monkeypatch.setattr(fused_ai.time, "sleep", lambda s: None)
    fused_ai._wait_job("sys:x", on_progress=lambda r: seen.append(r["state"]))
    assert seen == ["running", "done"]


def test_transcribe_wait_false_returns_the_immediate_reply(monkeypatch):
    reply = {"jobId": "sys:t:1", "path": "/tmp/a.wav", "output": "/tmp/a.json"}

    def fake_post_json(path, body, timeout=None):
        assert path == "/api/ai/transcribe"
        assert body["path"] == os.path.abspath("a.wav")
        return reply

    monkeypatch.setattr(fused_ai, "_post_json", fake_post_json)
    got = fused_ai.transcribe("a.wav", wait=False)
    assert got == reply


def test_transcribe_wait_true_raises_on_cancelled_job(monkeypatch):
    reply = {"jobId": "sys:t:1", "path": "/tmp/a.wav"}
    monkeypatch.setattr(fused_ai, "_post_json", lambda p, b, timeout=None: reply)
    monkeypatch.setattr(
        fused_ai, "_wait_job",
        lambda job_id, on_progress=None, timeout=None: {"state": "cancelled"})
    with pytest.raises(fused_ai.AiError) as exc:
        fused_ai.transcribe("a.wav")
    assert exc.value.type == "cancelled"


def test_image_wait_true_returns_reply_on_done(monkeypatch):
    reply = {"jobId": "sys:img:1", "path": "/tmp/x.png"}
    monkeypatch.setattr(fused_ai, "_post_json", lambda p, b, timeout=None: reply)
    monkeypatch.setattr(
        fused_ai, "_wait_job",
        lambda job_id, on_progress=None, timeout=None: {"state": "done"})
    result = fused_ai.image("a cat")
    # The one result frame (D632): the payload is `images`, the job id is
    # `response.id`, and nothing from the started reply sits at top level.
    assert result["images"] == [{"path": "/tmp/x.png", "mediaType": "image/png"}]
    assert result["response"]["id"] == "sys:img:1"
    assert result["usage"] == {"imagesGenerated": 1}
    assert "jobId" not in result and "path" not in result


def test_models_load_waits_by_default(monkeypatch):
    reply = {"jobId": "sys:ai-load:m", "model": "org/name", "state": "loading"}
    monkeypatch.setattr(fused_ai, "_post_json", lambda p, b, timeout=None: reply)
    monkeypatch.setattr(
        fused_ai, "_wait_job",
        lambda job_id, on_progress=None, timeout=None: {"state": "done"})
    assert fused_ai.models.load("org/name") == reply


# ------------------------------------------------------------------- embed


def test_embed_requires_exactly_one_of_texts_or_paths():
    with pytest.raises(fused_ai.AiError):
        fused_ai.embed()
    with pytest.raises(fused_ai.AiError):
        fused_ai.embed(texts=["a"], paths=["/tmp/a.png"])


def test_embed_returns_result_on_success(monkeypatch):
    payload = {"ok": True, "result": {"vectors": [[1.0]], "dim": 1, "model": "m"}}
    monkeypatch.setattr(fused_ai, "_post_json", lambda p, b, timeout=None: payload)
    got = fused_ai.embed(texts=["hi"])
    assert got["dim"] == 1


def test_embed_error_shape_raises_aierror(monkeypatch):
    payload = {"ok": False, "error": {"type": "model_loading", "message": "loading",
                                      "jobId": "sys:x"}}
    monkeypatch.setattr(fused_ai, "_post_json", lambda p, b, timeout=None: payload)
    with pytest.raises(fused_ai.AiError) as exc:
        fused_ai.embed(texts=["hi"])
    assert exc.value.type == "model_loading"
    assert exc.value.job_id == "sys:x"


# ------------------------------------------------------------------- cancel


def test_cancel_returns_bool(monkeypatch):
    monkeypatch.setattr(fused_ai, "_post_json",
                        lambda p, b, timeout=None: {"cancelled": True})
    assert fused_ai.cancel("text-generation") is True
    monkeypatch.setattr(fused_ai, "_post_json",
                        lambda p, b, timeout=None: {"cancelled": False})
    assert fused_ai.cancel() is False


# ----------------------------------------------------- drift pin (D413 x3)


def test_the_clients_image_wire_keys_match_the_servers_constant():
    from fused_render.server.routers import ai_runtime
    assert fused_ai._IMAGE_WIRE_KEYS == ai_runtime._IMAGE_OPTIONS


def test_the_clients_transcribe_wire_keys_match_the_servers_constant():
    from fused_render.server.routers import ai_runtime
    assert fused_ai._TRANSCRIBE_WIRE_KEYS == ai_runtime._TRANSCRIBE_OPTIONS


def test_the_clients_embed_wire_keys_match_the_servers_constant():
    """The third of the three pins, and `kind` is why it was written (SPEC §40).

    `kind` is refused per MODEL rather than per endpoint — a dual encoder has no
    retrieval convention — so the failure a drift here produces is not a 400 a
    caller can see: it is every retrieval model embedding queries as documents,
    which returns unit-length vectors of the right dimension and worse recall,
    with nothing measurable to say so. Exactly the class of bug the image and
    transcribe pins already exist for (D413 x3).
    """
    from fused_render.server.routers import ai_runtime
    assert fused_ai._EMBED_WIRE_KEYS == ai_runtime._EMBED_OPTIONS


def test_embed_forwards_kind_only_when_it_is_given_one():
    """An absent key is "I did not say" and the server applies its own default;
    an explicit one on a model with no retrieval convention is a 400. So sending
    a default from the client would turn every legal dual-encoder call into a
    refused one — the same reason `model` is conditional."""
    sent = []
    original = fused_ai._post_json
    try:
        fused_ai._post_json = lambda p, b, timeout=None: (
            sent.append(b), {"ok": True, "result": {"vectors": [[1.0]], "dim": 1}})[1]
        fused_ai.embed(texts=["a"])
        assert "kind" not in sent[0]
        fused_ai.embed(texts=["a"], kind="query")
        assert sent[1]["kind"] == "query"
    finally:
        fused_ai._post_json = original


def test_the_bridge_and_the_client_forward_the_same_embed_options():
    """`runtime.js` is the third copy of this surface, and it is JS — no import
    can pin it, so the assertion is over its source.

    Cheap and worth it: a page author meets `fused.ai.embed` through the bridge
    and a `.py` author through this module, and a parameter one forwards and the
    other drops is a feature that works in half the app. Read as text rather
    than parsed, the same way `frontend/.../repoCardControls.test.ts` pins the
    Local card's own conditions.
    """
    import pathlib

    runtime = (pathlib.Path(fused_ai.__file__).parents[2]
               / "static" / "runtime.js").read_text(encoding="utf-8")
    embed = runtime[runtime.index("function aiEmbed(opts)"):]
    embed = embed[:embed.index("/api/ai/embed")]
    for option in sorted(fused_ai._EMBED_WIRE_KEYS):
        assert f"opts.{option}" in embed or f"body.{option}" in embed, option
    # …and the one it must NOT forward from a caller's own options object:
    # `base` is bridge-injected from the page's own `?path=`, so a caller
    # passing it is passing an option that does not exist from where they stand.
    assert "opts.base" not in embed


def test_the_ai_object_mirrors_the_js_surface():
    assert callable(fused_ai.ai.text)
    assert callable(fused_ai.ai.stream)
    assert callable(fused_ai.ai.transcribe)
    assert callable(fused_ai.ai.image)
    assert callable(fused_ai.ai.embed)
    assert callable(fused_ai.ai.cancel)
    assert callable(fused_ai.ai.models.list)
    assert callable(fused_ai.ai.models.catalog)
    assert callable(fused_ai.ai.models.load)
    assert callable(fused_ai.ai.models.download)
    assert callable(fused_ai.ai.models.unload)


# ------------------------------------------- job-registry robustness (review)


def test_wait_job_tolerates_up_to_five_consecutive_misses(monkeypatch):
    """Mirrors runtime.js's watchJob: `_sweep` can drop a finished SERVER row
    on the very next `list_jobs()` above MAX_JOBS (SPEC AI-10a's queue case),
    so a single missing poll must not be fatal."""
    replies = iter([
        _jobs_payload({"id": "sys:x", "state": "running", "stalled": False}),
        _jobs_payload(),  # miss 1
        _jobs_payload(),  # miss 2
        _jobs_payload({"id": "sys:x", "state": "done", "stalled": False}),
    ])

    def fake_get_json(path, timeout=None):
        return next(replies)

    monkeypatch.setattr(fused_ai, "_get_json", fake_get_json)
    monkeypatch.setattr(fused_ai.time, "sleep", lambda s: None)
    record = fused_ai._wait_job("sys:x")
    assert record["state"] == "done"


def test_wait_job_raises_after_five_consecutive_misses_once_seen(monkeypatch):
    calls = {"n": 0}

    def flow(path, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _jobs_payload({"id": "sys:x", "state": "running", "stalled": False})
        return _jobs_payload()

    monkeypatch.setattr(fused_ai, "_get_json", flow)
    monkeypatch.setattr(fused_ai.time, "sleep", lambda s: None)
    with pytest.raises(fused_ai.AiError) as exc:
        fused_ai._wait_job("sys:x")
    assert exc.value.type == "error"
    # first poll (seen) + 5 misses = 6 calls total
    assert calls["n"] == 6


def test_wait_job_a_miss_before_ever_being_seen_does_not_count(monkeypatch):
    """The row may not exist yet on the very first poll right after the POST
    (upsert is synchronous server-side, but this is defence in depth) —
    misses only count once the row has been observed at least once."""
    calls = {"n": 0}

    def flow(path, timeout=None):
        calls["n"] += 1
        if calls["n"] <= 3:
            return _jobs_payload()  # not there yet
        return _jobs_payload({"id": "sys:x", "state": "done", "stalled": False})

    monkeypatch.setattr(fused_ai, "_get_json", flow)
    monkeypatch.setattr(fused_ai.time, "sleep", lambda s: None)
    record = fused_ai._wait_job("sys:x")
    assert record["state"] == "done"


def test_wait_job_does_not_raise_on_a_single_stalled_tick(monkeypatch):
    """A phase that ticks less often than STALE_AFTER_S (30s) marks the row
    stalled while the work continues — one stalled observation must not be
    fatal, only a PERSISTED one."""
    calls = {"n": 0}

    def flow(path, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _jobs_payload({"id": "sys:x", "state": "running", "stalled": True})
        return _jobs_payload({"id": "sys:x", "state": "done", "stalled": False})

    monkeypatch.setattr(fused_ai, "_get_json", flow)
    monkeypatch.setattr(fused_ai.time, "sleep", lambda s: None)
    record = fused_ai._wait_job("sys:x")
    assert record["state"] == "done"


def test_wait_job_raises_once_stalled_persists_past_the_grace_period(monkeypatch):
    ticks = iter([0.0, 10.0, 20.0, 100.0, 100.0])

    def fake_monotonic():
        return next(ticks, 200.0)

    def flow(path, timeout=None):
        return _jobs_payload({"id": "sys:x", "state": "running", "stalled": True})

    monkeypatch.setattr(fused_ai.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(fused_ai, "_get_json", flow)
    monkeypatch.setattr(fused_ai.time, "sleep", lambda s: None)
    with pytest.raises(fused_ai.AiError) as exc:
        fused_ai._wait_job("sys:x")
    assert exc.value.type == "stalled"


def test_models_load_short_circuits_on_an_already_ready_reply(monkeypatch):
    """`_start_resident`'s join branch answers `{"jobId", "model", "state":
    "ready"}` with NO `_report` call when the model is already resident and
    serving — the `sys:` job row may already be swept (FINISHED_TTL_S=30s),
    so waiting on it at all is the bug: the reply's own state must be
    trusted first."""
    reply = {"jobId": "sys:ai-model:org/name", "model": "org/name", "state": "ready"}
    monkeypatch.setattr(fused_ai, "_post_json", lambda p, b, timeout=None: reply)

    def boom(*a, **kw):
        raise AssertionError("must not poll /api/jobs for an already-ready model")

    monkeypatch.setattr(fused_ai, "_wait_job", boom)
    assert fused_ai.models.load("org/name") == reply


def test_models_load_still_waits_when_the_reply_says_its_loading(monkeypatch):
    reply = {"jobId": "sys:ai-model:org/name", "model": "org/name", "state": "downloading"}
    monkeypatch.setattr(fused_ai, "_post_json", lambda p, b, timeout=None: reply)
    monkeypatch.setattr(
        fused_ai, "_wait_job",
        lambda job_id, on_progress=None, timeout=None: {"state": "done"})
    assert fused_ai.models.load("org/name") == reply


def test_models_download_short_circuits_on_an_already_ready_reply(monkeypatch):
    reply = {"jobId": "sys:ai-model:org/name", "model": "org/name", "state": "ready"}
    monkeypatch.setattr(fused_ai, "_post_json", lambda p, b, timeout=None: reply)

    def boom(*a, **kw):
        raise AssertionError("must not poll /api/jobs for an already-ready model")

    monkeypatch.setattr(fused_ai, "_wait_job", boom)
    assert fused_ai.models.download("org/name") == reply


# ------------------------------------------------- appenv shadowing (review)


def test_fused_ai_loads_its_own_appenv_even_when_a_user_appenv_shadows_it(tmp_path, monkeypatch):
    """The shared dir is APPENDED to sys.path (by design — a user's own
    same-named module wins for user code), so `import appenv` from inside
    fused_ai.py must not resolve to a user-authored appenv.py placed earlier
    on sys.path — fused_ai.py must always load ITS OWN sibling."""
    user_appenv = tmp_path / "appenv.py"
    user_appenv.write_text("MARKER = 'user-owned'\n")  # no origin(), no home_dir()
    monkeypatch.syspath_prepend(str(tmp_path))
    # This test file already `import appenv`'d the real one at module scope,
    # which cached it in sys.modules under that bare name — a plain `import
    # appenv` below would just return the cached module regardless of
    # sys.path, proving nothing. Evict it so the import actually re-resolves.
    monkeypatch.delitem(sys.modules, "appenv", raising=False)
    try:
        import appenv as shadowing_appenv
        assert shadowing_appenv.__file__ == str(user_appenv)
        # The module under test must still resolve origin() etc. through its
        # OWN appenv, not raise AttributeError against the user's stand-in.
        assert fused_ai.appenv.origin is not None
        assert callable(fused_ai.appenv.origin)
        assert not hasattr(shadowing_appenv, "origin")
    finally:
        sys.modules.pop("appenv", None)
