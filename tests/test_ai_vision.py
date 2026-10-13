"""`fused.ai.vision` (SPEC AI-33): the route (stubbed supervisor), its client copies,
the recipes, and the `vision-torch` worker's torch-free logic."""
import contextlib
import importlib.util
import os
import re
import sys
import types

import pytest
from fastapi.testclient import TestClient

from fused_render import jobs
from fused_render.ai import catalog, registry, supervisor, tasks
from fused_render.ai.runners import formats
from fused_render.server import create_app
from fused_render.server.routers import ai_runtime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WORKER = os.path.join(_ROOT, "fused_render", "ai", "runners", "vision_torch", "worker.py")

MOONDREAM = "vikhyatk/moondream2"
DINO = "IDEA-Research/grounding-dino-tiny"


@pytest.fixture(autouse=True)
def _apple_silicon(monkeypatch):
    jobs.reset()
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    yield
    jobs.reset()


@pytest.fixture()
def client():
    return TestClient(create_app(start_dir="/"))


@pytest.fixture()
def photo(tmp_path):
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0not-really-a-jpeg")
    return str(path)


@pytest.fixture()
def served(monkeypatch):
    calls = []

    def fake(model, body, **requester):
        calls.append((model, body, requester))
        reply = {"task": body["task"], "width": 640, "height": 480, "seconds": 0.25}
        if body["task"] == "detect":
            reply["objects"] = [{"xMin": 0.1, "yMin": 0.2, "xMax": 0.3, "yMax": 0.4,
                                 "label": body["prompt"], "score": 0.9}]
        else:
            reply["text"] = "two cats"
        return reply

    monkeypatch.setattr(supervisor, "generate_vision", fake)
    return calls


def _post(client, body, **headers):
    return client.post("/api/ai/vision", json=body, headers={"X-Fused": "1", **headers})


# -- the route ------------------------------------------------------------------


@pytest.mark.parametrize("body, needle", [
    ({"task": "caption", "threshold": 0.3}, "threshold"),
    ({"task": "describe"}, "'task' must be one of"),
    ({"task": "caption", "prompt": "cats"}, "takes no 'prompt'"),
    ({"task": "detect"}, "needs a 'prompt'"),
    ({"task": "detect", "prompt": " ., ;"}, "naming what to look for"),
    ({"task": "detect", "prompt": "cat", "length": "short"}, "'length' is a caption option"),
    ({"task": "detect", "prompt": "cat", "maxObjects": 0}, "'maxObjects'"),
])
def test_a_malformed_request_is_a_bad_request(client, photo, served, body, needle):
    response = _post(client, {"image": photo, **body})
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "bad_request"
    assert needle in response.json()["error"]["message"]
    assert served == []


def test_vision_is_unavailable_on_claude_and_off_apple_silicon(client, photo, served, monkeypatch):
    assert _post(client, {"image": photo, "task": "caption", "provider": "claude"}).status_code in (400, 409)
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    response = _post(client, {"image": photo, "task": "caption"})
    assert (response.status_code, response.json()["error"]["type"]) == (409, "unavailable")
    assert served == []


def test_the_model_is_chosen_by_task(client, photo, served):
    assert _post(client, {"image": photo, "task": "detect", "prompt": "cat"}).status_code == 200
    assert _post(client, {"image": photo, "task": "caption"}).status_code == 200
    assert [model for model, _, _ in served] == [DINO, MOONDREAM]
    unable = _post(client, {"image": photo, "task": "caption", "model": DINO}).json()["error"]
    assert "cannot caption" in unable["message"] and MOONDREAM in unable["message"]
    stranger = _post(client, {"image": photo, "task": "caption", "model": "someone/moondream-ft"})
    assert "is not a vision model here" in stranger.json()["error"]["message"]


def test_a_reply_is_the_shared_frame(client, served, tmp_path):
    (tmp_path / "pics").mkdir()
    (tmp_path / "pics" / "a.jpg").write_bytes(b"x")
    response = _post(client, {"image": "pics/a.jpg", "base": str(tmp_path / "index.html"),
                              "task": "detect", "prompt": " cat ", "maxObjects": 3,
                              "model": MOONDREAM}, **{"X-Fused-Page": "/apps/demo/index.html"})
    assert response.status_code == 200
    model, body, requester = served[0]
    assert (model, body) == (MOONDREAM, {"task": "detect", "image": str(tmp_path / "pics" / "a.jpg"),
                                         "prompt": "cat", "maxObjects": 3})
    assert requester["requester_id"]
    result = response.json()["result"]
    assert result["imageSize"] == {"width": 640, "height": 480}
    assert result["objects"][0]["label"] == "cat" and "text" not in result
    assert result["response"]["modelId"] == MOONDREAM
    assert result["providerMetadata"]["local"] == {"runner": "vision-torch", "seconds": 0.25}


@pytest.mark.parametrize("raised, status, kind", [
    (lambda m: supervisor.ModelNotReady("loading", supervisor.job_id_for(m)), 409, "model_loading"),
    (lambda m: supervisor.ModelSlotConflict(registry.VISION, [m, DINO], DINO, held=False),
     409, "model_slot_conflict"),
    (lambda m: supervisor.SupervisorError("image not readable"), 502, "ai_error"),
])
def test_supervisor_failures_map_to_the_shared_errors(client, photo, monkeypatch, raised, status, kind):
    def fail(model, body, **requester):
        raise raised(model)

    monkeypatch.setattr(supervisor, "generate_vision", fail)
    response = _post(client, {"image": photo, "task": "caption"})
    assert (response.status_code, response.json()["error"]["type"]) == (status, kind)
    if kind == "model_loading":
        assert response.json()["error"]["jobId"] == supervisor.job_id_for(MOONDREAM)


# -- recipes and the catalog ----------------------------------------------------


def test_the_catalog_publishes_each_suggestions_recipe_tasks(client):
    assert all("tasks" not in e for e in catalog.SUGGESTIONS["vision-torch"])
    rows = client.get("/api/ai/catalog").json()["capabilities"]
    models = next(r for r in rows if r["capability"] == registry.VISION)["models"]
    assert {m["id"] for m in models} == {e["id"] for e in catalog.SUGGESTIONS["vision-torch"]}
    for model in models:
        assert model["tasks"] == list(formats.VISION_TORCH_RECIPES[model["id"]]["tasks"])


def test_recipes_are_curated_and_code_is_pinned():
    for repo_id, recipe in formats.VISION_TORCH_RECIPES.items():
        if any(p.endswith(".py") for p in recipe["allow"]):
            assert re.fullmatch(r"[0-9a-f]{40}", recipe.get("revision") or ""), repo_id
    assert formats.component("moondream/starmie-v1")["of"] == MOONDREAM
    layout = dict(names={"config.json", "model.safetensors"}, dirnames=set(), config={},
                  torch_weights=True)
    assert "vision-torch" not in formats.loaders(repo_id="someone/moondream-ft", **layout)
    for tag in ("image-to-text", "zero-shot-object-detection"):
        assert tasks.capability_for_tag(tag) == registry.VISION and tasks.is_format_gated(tag)


def test_the_clients_send_the_servers_options(monkeypatch, tmp_path):
    source = open(os.path.join(_ROOT, "fused_render", "static", "runtime.js"), encoding="utf-8").read()
    body = source[source.index("  function aiVision(opts)"):]
    keys = re.search(r'const visionKeys = \[(.*?)\];', body, re.S).group(1)
    assert sorted(re.findall(r'"([^"]+)"', keys)) == sorted(ai_runtime._VISION_OPTIONS)
    assert "body.base = ownPath" in body and "vision: aiVision" in source

    monkeypatch.syspath_prepend(os.path.join(_ROOT, "fused_render", "templates", "shared"))
    import fused_ai
    assert fused_ai._VISION_WIRE_KEYS == ai_runtime._VISION_OPTIONS
    sent = []
    monkeypatch.setattr(fused_ai, "_post_json", lambda path, body, timeout=None: sent.append(
        (path, body)) or {"ok": True, "result": {"task": "detect"}})
    monkeypatch.chdir(tmp_path)
    assert fused_ai.vision("a.jpg", "detect", "cat", max_objects=2) == {"task": "detect"}
    assert sent == [("/api/ai/vision", {"image": str(tmp_path / "a.jpg"), "task": "detect",
                                        "prompt": "cat", "maxObjects": 2})]


# -- the worker, without torch --------------------------------------------------


class _FakeTorch(types.ModuleType):
    """Only what `generate` touches; `inside` says whether a request is under inference_mode."""

    def __init__(self):
        super().__init__("torch")
        self.inside = False

    @contextlib.contextmanager
    def inference_mode(self):
        self.inside = True
        try:
            yield
        finally:
            self.inside = False


class _FakeBase(types.ModuleType):
    def __init__(self):
        super().__init__("worker_base")
        self.downloads = []

    def download_snapshot(self, model_id, allow_patterns=None, **kwargs):
        self.downloads.append((model_id, kwargs.get("revision")))
        return f"/snap/{model_id}/{kwargs.get('revision') or 'main'}"

    def download_file(self, repo_id, filename, **kwargs):
        self.downloads.append((repo_id, filename))


@pytest.fixture()
def worker(monkeypatch):
    monkeypatch.setitem(sys.modules, "worker_base", _FakeBase())
    monkeypatch.setitem(sys.modules, "formats", formats)
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch())
    spec = importlib.util.spec_from_file_location("vision_torch_worker_under_test", _WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_download_fetches_the_pinned_commit_and_load_refuses_any_other(worker):
    pin = formats.VISION_TORCH_RECIPES[MOONDREAM]["revision"]
    assert worker.download(MOONDREAM) == f"/snap/{MOONDREAM}/{pin}"
    worker.download(DINO)
    assert worker.worker_base.downloads == [
        ("moondream/starmie-v1", "tokenizer.json"), (MOONDREAM, pin), (DINO, None)]
    with pytest.raises(ValueError, match="not a vision model this runner knows"):
        worker.download("someone/moondream-ft")
    with pytest.raises(RuntimeError, match="not the pinned commit"):
        worker._Moondream(MOONDREAM, formats.vision_torch_recipe(MOONDREAM), f"/snap/x/{'c' * 40}")


@pytest.mark.parametrize("body, needle", [
    ({"task": "caption", "image": "a.jpg"}, "absolute"),
    ({"task": "detect", "image": "/a.jpg", "prompt": "."}, "needs a 'prompt'"),
    ({"task": "detect", "image": "/a.jpg", "prompt": "c", "maxObjects": -1}, "'maxObjects'"),
])
def test_the_worker_validates_its_own_body(worker, body, needle):
    with pytest.raises(ValueError, match=needle):
        worker.validate_request(body)


class _FakeMoondream:
    def caption(self, image, length):
        return {"caption": f"a {length} caption"}

    def query(self, image, question, reasoning):
        return {"answer": "two", "reasoning": {"text": "I count two"}}

    def detect(self, image, obj, settings):
        # Grad mode is per thread; set at load it never reached the request thread.
        assert sys.modules["torch"].inside and settings == {"max_objects": 50}
        return {"objects": [{"x_min": -0.01, "y_min": 0.2, "x_max": 1.0001, "y_max": 0.5}]}

    def point(self, image, obj, settings):
        return {"points": [{"x": 0.25, "y": 0.75}, {"x": 0.5, "y": 0.5}]}


def test_moondream_replies_are_normalized_and_tasks_are_enforced(worker):
    family = worker._Moondream.__new__(worker._Moondream)
    family.model, family._encode = _FakeMoondream(), lambda path: ("encoded", (640, 480))
    worker._loaded.update(family=family, model_id=MOONDREAM,
                          tasks=formats.VISION_TORCH_RECIPES[MOONDREAM]["tasks"])
    detect = worker.generate({"task": "detect", "image": "/a.jpg", "prompt": "cat"})
    assert detect["objects"] == [{"xMin": 0.0, "yMin": 0.2, "xMax": 1.0, "yMax": 0.5,
                                  "label": "cat", "score": None}]
    query = worker.generate({"task": "query", "image": "/a.jpg", "prompt": "how?", "reasoning": True})
    assert (query["text"], query["reasoning"]) == ("two", "I count two")
    points = worker.generate({"task": "point", "image": "/a.jpg", "prompt": "cat", "maxObjects": 1})
    assert points["points"] == [{"x": 0.25, "y": 0.75, "label": "cat"}]
    assert worker.generate({"task": "caption", "image": "/a.jpg", "length": "short"})["text"] == "a short caption"
    worker._loaded.update(tasks=("detect",))
    with pytest.raises(ValueError, match="cannot caption"):
        worker.generate({"task": "caption", "image": "/a.jpg"})


def test_the_worker_opens_a_rotated_photo_upright(worker, tmp_path):
    from PIL import Image

    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° clockwise to display
    Image.new("RGB", (40, 20)).save(tmp_path / "phone.jpg", exif=exif)
    assert worker._open(str(tmp_path / "phone.jpg")).size == (20, 40)
