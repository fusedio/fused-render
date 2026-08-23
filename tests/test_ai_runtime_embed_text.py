"""`POST /api/ai/embed-text` (SPEC §40) — the HTTP surface over the
text-embedding runners.

Modeled on `tests/test_ai_runtime_embed.py`, which covers the DUAL-ENCODER
endpoint, and split into its own file for the same reason that one was split
from `test_ai_runtime.py`: these two routes share a wire shape and serve two
different capabilities, so a file that tested both would keep having to say
which one each assertion was about.

**The most important thing asserted here is the SAMENESS.** The two routes'
error contract is identical on purpose — a page that has written the
`model_loading` retry loop once must not need a second one — so several tests
below assert against `/api/ai/embed`'s own behaviour rather than against a
literal, which is what makes a future divergence in either route fail here.
"""
import pytest
from fastapi.testclient import TestClient

from fused_render import jobs
from fused_render.ai import registry, supervisor
from fused_render.server import create_app


@pytest.fixture(autouse=True)
def _clean_jobs():
    jobs.reset()
    yield
    jobs.reset()


@pytest.fixture()
def client():
    return TestClient(create_app(start_dir="/"))


def _post(client, body):
    return client.post("/api/ai/embed-text", json=body, headers={"X-Fused": "1"})


# -- the X-Fused guard, shared with every other mutating POST -----------------


def test_the_x_fused_header_is_required(client):
    assert client.post("/api/ai/embed-text", json={"texts": ["a"]}).status_code == 403


# -- 400s: the same shape `text_embed_common.request_texts` enforces ----------


def test_an_empty_body_is_a_bad_request(client):
    response = _post(client, {})
    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["type"] == "bad_request"


def test_an_empty_list_is_a_bad_request(client):
    assert _post(client, {"texts": []}).status_code == 400


def test_a_non_string_item_is_a_bad_request(client):
    response = _post(client, {"texts": ["ok", 3]})
    assert response.status_code == 400
    assert "texts[1]" in response.json()["error"]["message"]


def test_over_64_items_is_a_bad_request(client):
    response = _post(client, {"texts": ["x"] * 65})
    assert response.status_code == 400
    assert "at most 64" in response.json()["error"]["message"]


def test_exactly_64_items_is_not_refused_for_its_SIZE(client, monkeypatch):
    """The off-by-one, pinned the same way the sibling route pins it."""
    monkeypatch.setattr(supervisor, "generate_embed",
                        lambda model, body, capability=None: {
                            "vectors": [[1.0]] * 64, "dim": 1})
    response = _post(client, {"texts": ["x"] * 64, "model": "org/e"})
    assert response.status_code == 200


def test_image_paths_are_refused_before_a_model_is_even_resolved(client):
    """**Deliverable: images are meaningless here and are refused clearly.**

    Refused as a 400 rather than ignored, and with a sentence that points at
    the endpoint which does take images — a caller who found
    `fused.ai.embed`'s image half and assumed this one had it would otherwise
    get the FILENAMES embedded as prose: plausible vectors, no error, nonsense
    results.

    No model is resolved and no worker is touched to reach this answer, which
    is the same ordering `/api/ai/embed` uses for its own validator: a
    malformed request must cost nothing rather than a 409 implying the fix is
    to wait.
    """
    response = _post(client, {"texts": ["a"], "paths": ["/photos/cat.png"]})
    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "fused.ai.embed()" in message
    assert "vision tower" in message


def test_an_unrecognised_kind_is_a_bad_request(client):
    """The silent-degradation guard, at the edge. See
    `text_embed_common.request_texts` for why this one field is refused
    rather than defaulted."""
    response = _post(client, {"texts": ["a"], "kind": "queries"})
    assert response.status_code == 400
    assert "'query'" in response.json()["error"]["message"]


# -- 409 unavailable ----------------------------------------------------------


def test_no_text_embedding_runner_says_why(client, monkeypatch):
    monkeypatch.setattr(registry, "_RUNNERS", ())
    response = _post(client, {"texts": ["a cat"]})
    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["type"] == "unavailable"


# -- 409 model_loading: the fork this endpoint shares with /api/ai ------------


def test_a_cold_model_answers_model_loading_with_a_job_id(client, monkeypatch):
    """**The contract the brief pins by name.** An embed call has no job of
    its own for a cold multi-MB load to hide inside, so it fails fast with the
    id of the load it just started — the `/api/ai` local-model fork, byte for
    byte the same as `/api/ai/embed`'s."""
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


def test_the_cold_load_is_started_under_the_TEXT_EMBEDDING_capability(
        client, monkeypatch):
    """The whole point of the capability split, checked where it would break.

    If this route started its load under `registry.EMBEDDINGS`, a text
    embedder would evict a resident SigLIP model and the two capabilities
    would share one slot — which is precisely the outcome the split exists to
    avoid, and which nothing else in this file would notice.
    """
    started = []
    monkeypatch.setattr(supervisor, "load",
                        lambda model, capability, **kw: started.append(
                            (model, capability)) or {"jobId": "j1"})
    response = _post(client, {"texts": ["a cat"], "model": "org/embed-x"})
    assert response.status_code == 409
    assert started == [("org/embed-x", registry.TEXT_EMBEDDINGS)]


# -- the happy path, with a stubbed worker ------------------------------------


def test_a_resident_model_answers_with_vectors_and_says_how(client, monkeypatch):
    """The reply carries `kind` and `promptScheme` back, and they are not
    decoration: both are decisions made on the caller's behalf that change
    what the vectors mean and that nothing downstream can detect — a
    wrongly-prompted batch still returns unit vectors of the right width."""
    calls = []

    def fake_generate_embed(model, body, capability=None):
        calls.append((model, body, capability))
        return {"vectors": [[0.6, 0.8]], "dim": 2, "kind": body["kind"],
                "promptScheme": "nomic"}

    monkeypatch.setattr(supervisor, "generate_embed", fake_generate_embed)
    response = _post(client, {"texts": ["a cat"], "model": "org/embed-x"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["result"] == {
        "vectors": [[0.6, 0.8]], "dim": 2, "model": "org/embed-x",
        "kind": "document", "promptScheme": "nomic"}
    assert calls == [("org/embed-x", {"texts": ["a cat"], "kind": "document"},
                      registry.TEXT_EMBEDDINGS)]


def test_the_kind_reaches_the_worker(client, monkeypatch):
    """`kind` is not a field the route interprets — it goes down to the runner,
    which is the only party that knows which prompt pair the loaded weights
    want."""
    calls = []
    monkeypatch.setattr(
        supervisor, "generate_embed",
        lambda model, body, capability=None: calls.append(body) or {
            "vectors": [[1.0]], "dim": 1, "kind": body["kind"],
            "promptScheme": "e5"})
    response = _post(client, {"texts": ["leaky roof"], "kind": "query",
                              "model": "org/e"})
    assert calls == [{"texts": ["leaky roof"], "kind": "query"}]
    assert response.json()["result"]["kind"] == "query"


def test_a_runtime_failure_is_ai_error(client, monkeypatch):
    def boom(model, body, capability=None):
        raise supervisor.SupervisorError("the model process did not answer")

    monkeypatch.setattr(supervisor, "generate_embed", boom)
    response = _post(client, {"texts": ["a"], "model": "org/e"})
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "ai_error"


# -- the two routes must not drift --------------------------------------------


def test_both_embedding_routes_share_one_error_contract(client, monkeypatch):
    """Asserted against the SIBLING ROUTE rather than against a literal, so a
    change to either one's error shape fails here.

    A page written against `fused.ai.embed`'s rejections — `.type`, and
    `.jobId` on a 409 — must be able to read `fused.ai.embedText`'s the same
    way, or the bridge's shared `embedPost` is a lie.
    """
    embed = client.post("/api/ai/embed", json={}, headers={"X-Fused": "1"})
    embed_text = _post(client, {})
    assert embed.status_code == embed_text.status_code == 400
    assert set(embed.json()) == set(embed_text.json())
    assert set(embed.json()["error"]) == set(embed_text.json()["error"])


def test_the_two_capabilities_hold_separate_resident_slots(client, monkeypatch):
    """The user-visible payoff of the split, asserted at the supervisor's own
    table rather than through the routes: one worker per CAPABILITY means a
    SigLIP model and a text embedder can be resident together."""
    started = []
    monkeypatch.setattr(supervisor, "load",
                        lambda model, capability, **kw: started.append(
                            (model, capability)) or {"jobId": "j"})
    client.post("/api/ai/embed", json={"texts": ["a"], "model": "org/siglip"},
                headers={"X-Fused": "1"})
    _post(client, {"texts": ["a"], "model": "org/bge"})
    assert started == [("org/siglip", registry.EMBEDDINGS),
                       ("org/bge", registry.TEXT_EMBEDDINGS)]
