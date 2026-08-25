"""`POST /api/ai/embed` (SPEC §40) — the HTTP surface over the embedding
runners, modeled closely on `/api/ai/image`'s own tests in
`test_ai_runtime.py`, but split into its own file: the wire shape here is
`/api/ai`'s (`{ok, result|error:{type,message,jobId}}`), not `/api/ai/image`'s
plain `{error}`, and keeping the two apart is the whole point of the fixtures
below.
"""
import os
import pytest
from fastapi.testclient import TestClient

from fused_render import jobs
from fused_render.ai import registry, supervisor
from fused_render.server import create_app
from fused_render.server.routers import ai_runtime


@pytest.fixture(autouse=True)
def _clean_jobs():
    jobs.reset()
    yield
    jobs.reset()


@pytest.fixture()
def client():
    return TestClient(create_app(start_dir="/"))


def _post(client, body):
    return client.post("/api/ai/embed", json=body, headers={"X-Fused": "1"})


# -- the X-Fused guard, shared with every other mutating POST -------------------


def test_the_x_fused_header_is_required(client):
    response = client.post("/api/ai/embed", json={"texts": ["a"]})
    assert response.status_code == 403


# -- the envelope check: caught before request_kind ever reads a field ----------


def test_a_misspelled_kind_key_is_a_bad_request_not_a_silent_default(client):
    """`request_kind` only ever reads `body.get("kind")` — a typo'd key like
    `kimd` is invisible to it, `kind` reads back `None`, and the request
    silently defaults to `DEFAULT_KIND` ("document"). That is the one field
    this whole endpoint argues hardest about (SPEC §40, PY-19): a retrieval
    model given the wrong half of its prompt pair returns vectors that are
    unit length, correctly shaped, and wrong neighbours, with nothing to show
    it. The envelope check must catch the misspelling before `request_kind`
    gets a chance to default it away."""
    response = _post(client, {"texts": ["a cat"], "kimd": "query"})
    assert response.status_code == 400
    assert "kimd" in response.json()["error"]["message"]


def test_an_unrecognised_embed_option_is_a_bad_request(client):
    response = _post(client, {"texts": ["a cat"], "bogus": 1})
    assert response.status_code == 400
    assert "bogus" in response.json()["error"]["message"]


def test_the_server_accepts_base_the_bridge_would_have_injected(client, monkeypatch):
    """`base` is bridge-injected (RH-1) — the SERVER's accepted set is wider
    than the caller-facing one on purpose, same asymmetry `/api/ai/image` and
    `/api/ai/transcribe` carry. Passing it must not itself 400; the request is
    left to fail downstream for an unrelated, easily distinguished reason."""
    monkeypatch.setattr(registry, "_RUNNERS", ())
    response = _post(client, {"texts": ["a cat"], "base": "/some/page.html"})
    assert response.status_code != 400


# -- 400s: the same shape `embed_common.request_kind` enforces ------------------


def test_an_empty_body_is_a_bad_request(client):
    response = _post(client, {})
    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["type"] == "bad_request"


def test_both_texts_and_paths_is_a_bad_request(client):
    response = _post(client, {"texts": ["a"], "paths": ["/a.png"]})
    assert response.status_code == 400
    assert "not both" in response.json()["error"]["message"]


def test_an_empty_list_is_a_bad_request(client):
    response = _post(client, {"texts": []})
    assert response.status_code == 400


def test_over_64_items_is_a_bad_request(client):
    response = _post(client, {"texts": ["x"] * 65})
    assert response.status_code == 400
    assert "64" in response.json()["error"]["message"]


def test_exactly_64_items_is_not_refused_for_its_SIZE(client, monkeypatch):
    """Distinguishes the batch-size check from everything downstream: a 64-item
    batch must clear `request_kind` and reach model resolution, where this
    test lets it fail for an unrelated, easily distinguished reason."""
    monkeypatch.setattr(registry, "_RUNNERS", ())
    response = _post(client, {"texts": ["x"] * 64})
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "unavailable"


def test_a_non_string_item_is_a_bad_request(client):
    response = _post(client, {"texts": ["fine", 42]})
    assert response.status_code == 400
    assert "texts[1]" in response.json()["error"]["message"]


# -- 409 unavailable: no runner and no curated default ---------------------------


def test_no_embedding_runner_says_why(client, monkeypatch):
    monkeypatch.setattr(registry, "_RUNNERS", ())
    response = _post(client, {"texts": ["a cat"]})
    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["type"] == "unavailable"


# -- 409 model_loading: the fork this endpoint takes instead of image/transcribe -


def test_a_cold_model_answers_model_loading_with_a_job_id(client, monkeypatch):
    """The `/api/ai` local-model fork (`test_a_slash_bearing_model_goes_local`
    in `test_ai_runtime.py`), not `/api/ai/image`'s wait-inside-the-job one: an
    embed call has no job of its own for a cold multi-GB load to hide inside,
    so it fails fast with the id of the load it just started."""
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(supervisor, "_require_build_tools", lambda: None)
    try:
        response = _post(client, {"texts": ["a cat"], "model": "org/embed-x"})
        assert response.status_code == 409
        body = response.json()
        assert body["ok"] is False
        assert body["error"]["type"] == "model_loading"
        assert body["error"]["jobId"] == supervisor.job_id_for("org/embed-x")
    finally:
        supervisor.unload(model="org/embed-x")


# -- the happy path, with a stubbed worker ---------------------------------------


def test_a_resident_model_answers_with_vectors(client, monkeypatch):
    calls = []

    def fake_generate_embed(model, body):
        calls.append((model, body))
        return {"vectors": [[0.6, 0.8]], "dim": 2}

    monkeypatch.setattr(supervisor, "generate_embed", fake_generate_embed)
    response = _post(client, {"texts": ["a cat"], "model": "org/embed-x"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["result"] == {
        "vectors": [[0.6, 0.8]], "dim": 2, "model": "org/embed-x"}
    # `kind` is forwarded RESOLVED — the route decides it and the worker
    # validates the same value again, so the two cannot disagree about what a
    # caller who said nothing meant.
    assert calls == [("org/embed-x", {"texts": ["a cat"], "kind": "document"})]


def test_paths_are_forwarded_by_kind_not_texts(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        supervisor, "generate_embed",
        lambda model, body: (calls.append((model, body)),
                             {"vectors": [[1.0]], "dim": 1})[1])
    response = _post(client, {"paths": ["/tmp/pic.png"], "model": "org/embed-x"})
    assert response.status_code == 200
    assert calls == [("org/embed-x", {"paths": [os.path.abspath("/tmp/pic.png")]})]


def test_an_absolute_path_is_forwarded_untouched(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        supervisor, "generate_embed",
        lambda model, body: (calls.append(body),
                             {"vectors": [[1.0]], "dim": 1})[1])
    _post(client, {"paths": ["/abs/pic.png"], "model": "org/embed-x"})
    # Through `os.path.abspath`, so the one honest expectation is the OS's own
    # spelling — on Windows an absolute POSIX path gains the current drive.
    assert calls == [{"paths": [os.path.abspath("/abs/pic.png")]}]


def test_a_relative_path_needs_a_base(client):
    response = _post(client, {"paths": ["pic.png"], "model": "org/embed-x"})
    assert response.status_code == 400
    assert "base" in response.json()["error"]["message"]


def test_a_relative_path_resolves_beside_base(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        supervisor, "generate_embed",
        lambda model, body: (calls.append(body),
                             {"vectors": [[1.0]], "dim": 1})[1])
    _post(client, {"paths": ["pic.png"], "model": "org/embed-x",
                   "base": "/pages/view.html"})
    assert calls == [{"paths": [os.path.abspath(os.path.join("/pages", "pic.png"))]}]


def test_a_runtime_failure_is_ai_error(client, monkeypatch):
    def boom(model, body):
        raise supervisor.SupervisorError("the model process did not answer")

    monkeypatch.setattr(supervisor, "generate_embed", boom)
    response = _post(client, {"texts": ["a cat"], "model": "org/embed-x"})
    assert response.status_code == 502
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["type"] == "ai_error"


# -- the two per-model refusals (SPEC §40) --------------------------------------
#
# `paths` needs a vision tower and `kind` needs a retrieval convention, and
# neither is a property of the REQUEST — both are facts about the model, so both
# are asked after `default_for` has resolved one. Ported in shape from PR #780's
# `tests/test_ai_runtime_embed_text.py`.
#
# **Refused, never ignored.** A `paths` request a text encoder accepted would
# embed noise; a `kind` a dual encoder accepted would be a parameter with no
# effect. Both return unit-length vectors of the right dimension either way, so
# nothing downstream can tell.


@pytest.fixture()
def served(monkeypatch):
    """`supervisor.generate_embed`, recording what reached it."""
    calls = []
    monkeypatch.setattr(
        supervisor, "generate_embed",
        lambda model, body: (calls.append((model, body)),
                             {"vectors": [[0.6, 0.8]], "dim": 2})[1])
    return calls


def _families(monkeypatch, answers):
    """`hub_cache.embed_family` as the route sees it, per model id.

    Patched on the ROUTER's own name rather than on `hub_cache`, because the
    module imports the function directly (`from ... import embed_family`) and a
    patch on the source would not be seen — the same reason the image tests
    patch `ai_runtime._accepts_image`'s inputs where they are read.
    """
    monkeypatch.setattr(ai_runtime, "embed_family", answers.get)


def test_paths_on_a_TEXT_encoder_is_refused_by_name(client, monkeypatch, served):
    _families(monkeypatch, {"BAAI/bge-base-en-v1.5": "text"})
    response = _post(client, {"paths": ["/tmp/pic.png"],
                              "model": "BAAI/bge-base-en-v1.5"})
    assert response.status_code == 400
    message = response.json()["error"]["message"]
    # The MODEL, because the fix is to name a different one — and what to pass
    # instead, because "not supported" is not actionable.
    assert "BAAI/bge-base-en-v1.5" in message
    assert "texts" in message
    assert "vision tower" in message
    assert served == [], "nothing may reach the worker after a refusal"


def test_paths_on_a_DUAL_encoder_is_served(client, monkeypatch, served):
    _families(monkeypatch, {"onnx-community/siglip2-base-patch16-384-ONNX": "dual"})
    response = _post(client, {
        "paths": ["/tmp/pic.png"],
        "model": "onnx-community/siglip2-base-patch16-384-ONNX"})
    assert response.status_code == 200
    assert len(served) == 1
    # …and `kind` is NOT forwarded on an image request: the worker refuses the
    # pair outright, so sending it would turn a legal call into a 500.
    assert "kind" not in served[0][1]


def test_paths_on_a_COLD_model_still_answers_model_loading(client, monkeypatch):
    """**The reason the route's refusal is keyed on positive evidence rather
    than on `_accepts_paths`.** A model with no snapshot on disk has no config
    to read, so `embed_family` answers None — and this call must still start the
    download rather than being refused for a file that is not there yet.
    """
    _families(monkeypatch, {})

    def not_ready(model, body):
        raise supervisor.ModelNotReady("loading", job_id="sys:ai-load:x")

    monkeypatch.setattr(supervisor, "generate_embed", not_ready)
    response = _post(client, {"paths": ["/tmp/pic.png"], "model": "org/cold"})
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "model_loading"


def test_kind_on_a_model_with_NO_SCHEME_is_refused_by_name(client, monkeypatch,
                                                           served):
    """A SigLIP has no query/passage convention, so `kind` would change nothing
    about the vectors — which is exactly why it must not be accepted: a caller
    who passed it believes it did something."""
    _families(monkeypatch, {"onnx-community/siglip2-base-patch16-384-ONNX": "dual"})
    response = _post(client, {
        "texts": ["a cat"], "kind": "query",
        "model": "onnx-community/siglip2-base-patch16-384-ONNX"})
    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "onnx-community/siglip2-base-patch16-384-ONNX" in message
    assert "kind" in message
    assert served == []


def test_kind_on_a_RETRIEVAL_encoder_is_served_and_forwarded(client, monkeypatch,
                                                             served):
    _families(monkeypatch, {"BAAI/bge-base-en-v1.5": "text"})
    response = _post(client, {"texts": ["red shoes"], "kind": "query",
                              "model": "BAAI/bge-base-en-v1.5"})
    assert response.status_code == 200
    assert served == [("BAAI/bge-base-en-v1.5",
                       {"texts": ["red shoes"], "kind": "query"})]


def test_an_absent_kind_is_never_refused_even_on_a_model_with_no_scheme(
        client, monkeypatch, served):
    """The refusal is on the caller SAYING it, not on the default. Every bare
    call carries `kind: "document"` by the time it reaches the worker, and
    refusing that would break every SigLIP request in the app."""
    _families(monkeypatch, {"onnx-community/siglip2-base-patch16-384-ONNX": "dual"})
    response = _post(client, {
        "texts": ["a cat"],
        "model": "onnx-community/siglip2-base-patch16-384-ONNX"})
    assert response.status_code == 200
    assert served[0][1]["kind"] == "document"


def test_an_explicit_null_kind_is_not_refused_either(client, monkeypatch, served):
    """A page building its body with `kind: state || null` sends the null, and
    that is "I did not say" rather than a value."""
    _families(monkeypatch, {"onnx-community/siglip2-base-patch16-384-ONNX": "dual"})
    response = _post(client, {
        "texts": ["a cat"], "kind": None,
        "model": "onnx-community/siglip2-base-patch16-384-ONNX"})
    assert response.status_code == 200


def test_a_bad_kind_VALUE_is_refused_before_the_model_is_even_resolved(client,
                                                                      served):
    """`embed_common.request_kind`'s own refusal, which fires first — so a typo
    costs nothing rather than a 409 that implies the fix is to wait."""
    response = _post(client, {"texts": ["a"], "kind": "queries"})
    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "'query'" in message and "'document'" in message
    assert served == []


def test_kind_beside_paths_is_refused_by_the_shared_validator(client, served):
    response = _post(client, {"paths": ["/tmp/a.png"], "kind": "query",
                              "model": "org/x"})
    assert response.status_code == 400
    assert "kind" in response.json()["error"]["message"]
    assert served == []


# -- the catalog fields the Playground draws off -------------------------------


def test_the_catalog_publishes_acceptsPaths_and_promptScheme(client, monkeypatch):
    """Both fields on EVERY embeddings entry, computed per entry — a picker
    filtering on absence would offer a control the route then refuses, which is
    the failure these fields exist to prevent."""
    monkeypatch.setattr(
        ai_runtime, "embed_family",
        lambda model_id: "dual" if "siglip" in model_id else "text")
    rows = client.get("/api/ai/catalog").json()["capabilities"]
    embeddings = next(r for r in rows if r["capability"] == "embeddings")
    assert embeddings["models"], "the embeddings row must offer something"
    for entry in embeddings["models"]:
        assert "acceptsPaths" in entry
        assert "promptScheme" in entry
        if "siglip" in entry["id"]:
            assert entry["acceptsPaths"] is True
            # A dual encoder has no retrieval convention, and `"none"` travels
            # as None so a frontend truthiness test agrees with the route.
            assert entry["promptScheme"] is None
        else:
            assert entry["acceptsPaths"] is False
            assert entry["promptScheme"] in ("bge", "e5", "nomic")


def test_no_other_capability_claims_either_field(client):
    """`_accepts_image`'s rule restated: treating "refuses nothing" as evidence
    would have every text and speech entry claiming it takes a photo."""
    rows = client.get("/api/ai/catalog").json()["capabilities"]
    for row in rows:
        if row["capability"] == "embeddings":
            continue
        for entry in row["models"]:
            assert entry["acceptsPaths"] is False, entry["id"]
            assert entry["promptScheme"] is None, entry["id"]


# -- the stranded MODEL: refused, not a traceback (PR #830 regression) ---------
#
# A model whose only curated home is an engine that cannot run here reached
# `onnx_embed.download()` and raised. That RuntimeError is a correct last-resort
# guard and stays where it is — but by the time it fires a job row has opened, a
# venv may have been built, and the user is reading a traceback. These pin the
# earlier, honest refusal.
#
# Entry points, all four of which land on the same `catalog.engine_gap` answer:
# the Local tab's resume (`/api/ai/runtime/download`), a Load
# (`/api/ai/runtime/load`), and a page or exported app naming the id directly
# (`fused.ai.embed({model})` -> `/api/ai/embed`).


def _linux(monkeypatch):
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")


# The BASE row: patch16 upstream and patch16 export, one checkpoint, so the
# refusal can name a counterpart. The so400m rows are no longer a pair (patch16
# conversion against a patch14 export) and take `engine_gap`'s no-counterpart
# sentence instead — `test_ai_catalog_embeddings.py` covers that half.
MLX_ONLY = "google/siglip2-base-patch16-384"
COUNTERPART = "onnx-community/siglip2-base-patch16-384-ONNX"


def test_a_model_only_MLX_can_read_is_refused_with_the_engine_named(
        client, monkeypatch, served):
    """The embed route. `unavailable` rather than `bad_request`: the request is
    well formed and the answer is a fact about this machine — the same type this
    route already uses when nothing serves the capability at all."""
    _linux(monkeypatch)
    response = _post(client, {"texts": ["a cat"], "model": MLX_ONLY})
    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["type"] == "unavailable"
    message = body["error"]["message"]
    assert MLX_ONLY in message
    assert "MLX Embeddings" in message
    assert COUNTERPART in message
    assert served == [], "nothing may reach the worker after a refusal"


def test_the_download_route_refuses_the_same_model_the_same_way(client,
                                                                monkeypatch):
    """**The one that matters most**, because it is the Local tab's resume and
    because fetching the files is the operation a format check structurally
    cannot guard: there is nothing on disk to judge until it has run."""
    _linux(monkeypatch)
    started = []
    monkeypatch.setattr(
        supervisor, "load",
        lambda *a, **kw: (started.append(a), {"jobId": "sys:x"})[1])
    response = client.post("/api/ai/runtime/download",
                           json={"model": MLX_ONLY, "capability": "embeddings"},
                           headers={"X-Fused": "1"})
    assert response.status_code == 409
    message = response.json()["error"]
    assert MLX_ONLY in message and COUNTERPART in message
    assert started == [], "the download must not start"


def test_the_load_route_refuses_it_too(client, monkeypatch):
    _linux(monkeypatch)
    started = []
    monkeypatch.setattr(
        supervisor, "load",
        lambda *a, **kw: (started.append(a), {"jobId": "sys:x"})[1])
    response = client.post("/api/ai/runtime/load",
                           json={"model": MLX_ONLY, "capability": "embeddings"},
                           headers={"X-Fused": "1"})
    assert response.status_code == 409
    assert "MLX Embeddings" in response.json()["error"]
    assert started == []


def test_the_curated_ONNX_default_is_not_refused(client, monkeypatch, served):
    """The guard against the fix becoming a wall: the model this machine SHOULD
    load must sail straight through."""
    _linux(monkeypatch)
    response = _post(client, {"texts": ["a cat"],
                              "model": "nomic-ai/nomic-embed-text-v1.5"})
    assert response.status_code == 200
    assert len(served) == 1


def test_an_uncurated_model_is_not_refused_either(client, monkeypatch, served):
    """"No information" is not "no". A repo the user found in Discover has no
    curated home, so nothing here has an opinion about it and the runner's own
    format check stays the judge — exactly as before this fix."""
    _linux(monkeypatch)
    response = _post(client, {"texts": ["a cat"], "model": "someone/found-this"})
    assert response.status_code == 200
    assert len(served) == 1


def test_on_a_MAC_the_same_model_is_served_rather_than_refused(client,
                                                               monkeypatch,
                                                               served):
    """**The Mac-path pin.** The refusal is a fact about the machine, not about
    the model — on Apple Silicon `mlx-embed` serves and this is a perfectly good
    id. A fix that refused it everywhere would have taken the capability away
    from the platform it works best on."""
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    response = _post(client, {"texts": ["a cat"], "model": MLX_ONLY})
    assert response.status_code == 200
    assert len(served) == 1
