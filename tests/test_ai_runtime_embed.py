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
    assert calls == [("org/embed-x", {"texts": ["a cat"]})]


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
