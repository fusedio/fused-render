"""`fused.ai.vision` (SPEC AI-33): the `/api/ai/vision` route, its client
copies, the catalog's per-model `tasks`, and the `vision-torch` worker's
torch-free logic. The route is served by a stubbed `supervisor.generate_vision`,
the embed tests' pattern.
"""
import contextlib
import importlib.util
import os
import re
import sys
import types

import pytest
from fastapi.testclient import TestClient

from fused_render import jobs
from fused_render.ai import catalog, registry, supervisor
from fused_render.ai.runners import formats
from fused_render.server import create_app
from fused_render.server.routers import ai_runtime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHARED_DIR = os.path.join(_ROOT, "fused_render", "templates", "shared")
_WORKER = os.path.join(_ROOT, "fused_render", "ai", "runners", "vision_torch", "worker.py")

MOONDREAM = "vikhyatk/moondream2"
DINO = "IDEA-Research/grounding-dino-tiny"


@pytest.fixture(autouse=True)
def _clean_jobs():
    jobs.reset()
    yield
    jobs.reset()


@pytest.fixture(autouse=True)
def _apple_silicon(monkeypatch):
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")


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
        calls.append((model, body))
        reply = {"task": body["task"], "width": 640, "height": 480, "seconds": 0.25}
        if body["task"] in ("caption", "query"):
            reply["text"] = "two cats"
        elif body["task"] == "detect":
            reply["objects"] = [{"xMin": 0.1, "yMin": 0.2, "xMax": 0.3, "yMax": 0.4,
                                 "label": body["prompt"], "score": 0.9}]
        elif body["task"] == "point":
            reply["points"] = [{"x": 0.5, "y": 0.5, "label": body["prompt"]}]
        return reply

    monkeypatch.setattr(supervisor, "generate_vision", fake)
    return calls


def _post(client, body):
    return client.post("/api/ai/vision", json=body, headers={"X-Fused": "1"})


def _error(response):
    body = response.json()
    assert body["ok"] is False
    return body["error"]


# -- envelope and validation ----------------------------------------------------


def test_the_x_fused_header_is_required(client, photo):
    assert client.post("/api/ai/vision", json={"image": photo, "task": "caption"}).status_code == 403


def test_an_unknown_option_is_named(client, photo):
    response = _post(client, {"image": photo, "task": "caption", "threshold": 0.3})
    assert response.status_code == 400
    assert "threshold" in _error(response)["message"]


@pytest.mark.parametrize("body, needle", [
    ({"task": "describe"}, "'task' must be one of"),
    ({"task": "caption", "prompt": "cats"}, "takes no 'prompt'"),
    ({"task": "detect"}, "needs a 'prompt'"),
    ({"task": "query", "prompt": "  "}, "the question"),
    ({"task": "detect", "prompt": " ., ;"}, "naming what to look for"),
    ({"task": "detect", "prompt": "cat", "length": "short"}, "'length' is a caption option"),
    ({"task": "caption", "length": "huge"}, "'length' is a caption option"),
    ({"task": "query", "prompt": "q", "reasoning": "yes"}, "'reasoning'"),
    ({"task": "caption", "maxObjects": 3}, "'maxObjects'"),
    ({"task": "detect", "prompt": "cat", "maxObjects": 0}, "'maxObjects'"),
    ({"task": "detect", "prompt": "cat", "maxObjects": True}, "'maxObjects'"),
])
def test_a_malformed_request_is_a_bad_request(client, photo, served, body, needle):
    response = _post(client, {"image": photo, **body})
    assert response.status_code == 400
    error = _error(response)
    assert error["type"] == "bad_request"
    assert needle in error["message"]
    assert served == []


def test_a_missing_image_is_a_bad_request(client, served, tmp_path):
    response = _post(client, {"image": str(tmp_path / "gone.jpg"), "task": "caption"})
    assert response.status_code == 400
    assert "no such file" in _error(response)["message"]


def test_a_relative_image_resolves_beside_the_page(client, served, tmp_path):
    (tmp_path / "pics").mkdir()
    (tmp_path / "pics" / "a.jpg").write_bytes(b"x")
    page = tmp_path / "index.html"
    response = _post(client, {"image": "pics/a.jpg", "task": "caption", "base": str(page)})
    assert response.status_code == 200
    assert served[0][1]["image"] == str(tmp_path / "pics" / "a.jpg")


def test_claude_does_not_serve_vision(client, photo, served):
    response = _post(client, {"image": photo, "task": "caption", "provider": "claude"})
    assert response.status_code in (400, 409)
    assert served == []


def test_off_apple_silicon_vision_is_unavailable(client, photo, served, monkeypatch):
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    response = _post(client, {"image": photo, "task": "caption"})
    assert response.status_code == 409
    assert _error(response)["type"] == "unavailable"


# -- choosing a model by task ---------------------------------------------------


def test_no_model_picks_the_first_suggestion_that_can_do_the_task(client, photo, served):
    assert _post(client, {"image": photo, "task": "detect", "prompt": "cat"}).status_code == 200
    assert _post(client, {"image": photo, "task": "caption"}).status_code == 200
    assert [model for model, _ in served] == [DINO, MOONDREAM]


def test_a_model_that_cannot_do_the_task_names_one_that_can(client, photo, served):
    response = _post(client, {"image": photo, "task": "caption", "model": DINO})
    assert response.status_code == 400
    message = _error(response)["message"]
    assert "cannot caption" in message and MOONDREAM in message
    assert served == []


def test_an_uncurated_model_is_refused_by_name(client, photo, served):
    response = _post(client, {"image": photo, "task": "caption", "model": "someone/moondream-ft"})
    assert response.status_code == 400
    assert "is not a vision model here" in _error(response)["message"]


# -- the supervisor's answers ---------------------------------------------------


def test_a_cold_model_answers_model_loading_with_its_job(client, photo, monkeypatch):
    def cold(model, body, **requester):
        raise supervisor.ModelNotReady(f"{model} is loading now", supervisor.job_id_for(model))

    monkeypatch.setattr(supervisor, "generate_vision", cold)
    response = _post(client, {"image": photo, "task": "caption"})
    assert response.status_code == 409
    error = _error(response)
    assert error["type"] == "model_loading"
    assert error["jobId"] == supervisor.job_id_for(MOONDREAM)


def test_a_slot_conflict_is_terminal_and_names_the_contenders(client, photo, monkeypatch):
    def contended(model, body, **requester):
        raise supervisor.ModelSlotConflict(registry.VISION, [MOONDREAM, DINO], DINO, held=False)

    monkeypatch.setattr(supervisor, "generate_vision", contended)
    response = _post(client, {"image": photo, "task": "caption"})
    assert response.status_code == 409
    error = _error(response)
    assert error["type"] == "model_slot_conflict"
    assert MOONDREAM in error["message"] and DINO in error["message"]


def test_the_page_is_the_requester(client, photo, monkeypatch):
    seen = []

    def fake(model, body, **requester):
        seen.append(requester)
        return {"task": "caption", "width": 1, "height": 1, "text": "x"}

    monkeypatch.setattr(supervisor, "generate_vision", fake)
    client.post("/api/ai/vision", json={"image": photo, "task": "caption"},
                headers={"X-Fused": "1", "X-Fused-Page": "/apps/demo/index.html"})
    assert seen and seen[0]["requester_id"]


def test_a_worker_failure_is_an_ai_error(client, photo, monkeypatch):
    def broken(model, body, **requester):
        raise supervisor.SupervisorError("image not readable")

    monkeypatch.setattr(supervisor, "generate_vision", broken)
    response = _post(client, {"image": photo, "task": "caption"})
    assert response.status_code == 502
    assert _error(response)["type"] == "ai_error"


def test_a_detect_reply_is_the_shared_frame(client, photo, served):
    response = _post(client, {"image": photo, "task": "detect", "prompt": " cat ",
                              "maxObjects": 3, "model": MOONDREAM})
    assert response.status_code == 200
    result = response.json()["result"]
    assert served == [(MOONDREAM, {"task": "detect", "image": photo, "prompt": "cat",
                                   "maxObjects": 3})]
    assert result["task"] == "detect"
    assert result["imageSize"] == {"width": 640, "height": 480}
    assert result["objects"][0]["label"] == "cat"
    assert "text" not in result
    assert result["provider"] == "local"
    assert result["response"]["modelId"] == MOONDREAM
    assert result["providerMetadata"]["local"] == {"runner": "vision-torch", "seconds": 0.25}


def test_a_query_forwards_reasoning(client, photo, served):
    response = _post(client, {"image": photo, "task": "query", "prompt": "how many?",
                              "reasoning": True})
    assert response.status_code == 200
    assert served[0][1]["reasoning"] is True
    assert response.json()["result"]["text"] == "two cats"


# -- the catalog and the format table agree -------------------------------------


def test_every_vision_suggestion_is_a_recipe_and_the_catalog_publishes_its_tasks(client):
    for entry in catalog.SUGGESTIONS["vision-torch"]:
        assert formats.vision_torch_recipe(entry["id"]) is not None, entry["id"]
        assert "tasks" not in entry, "tasks come from the recipe, not a second copy"
    rows = client.get("/api/ai/catalog").json()["capabilities"]
    models = next(r for r in rows if r["capability"] == registry.VISION)["models"]
    assert models
    for model in models:
        assert model["tasks"] == list(formats.VISION_TORCH_RECIPES[model["id"]]["tasks"])
        assert set(model["tasks"]) <= set(ai_runtime._VISION_TASKS)


def test_a_recipe_carries_no_download_arguments():
    # A revision or any other kwarg takes download_snapshot off its cache fast path and the mirror.
    for repo_id, recipe in formats.VISION_TORCH_RECIPES.items():
        assert set(recipe) == {"family", "allow", "tasks"}, repo_id


def test_moondreams_tokenizer_is_a_registered_component():
    row = formats.component("moondream/starmie-v1")
    assert row is not None and row["of"] == MOONDREAM and row["file"] == "tokenizer.json"


def test_only_curated_repos_are_claimed_by_the_vision_runner():
    layout = dict(names={"config.json", "model.safetensors"}, dirnames=set(),
                  config={"architectures": ["HfMoondream"]}, torch_weights=True)
    assert formats.loaders(repo_id=MOONDREAM, **layout) == ("vision-torch",)
    assert "vision-torch" not in formats.loaders(repo_id="someone/moondream-ft", **layout)


def test_both_vision_tags_map_to_the_capability_and_are_gated():
    from fused_render.ai import tasks

    for tag in ("image-to-text", "zero-shot-object-detection"):
        assert tasks.capability_for_tag(tag) == registry.VISION
        assert tasks.is_format_gated(tag)


# -- the client copies of the option list ---------------------------------------


def test_the_bridges_vision_keys_match_the_servers_constant():
    source = open(os.path.join(_ROOT, "fused_render", "static", "runtime.js"),
                  encoding="utf-8").read()
    start = source.index("  function aiVision(opts)")
    body = source[start:source.index("\n  }\n", start)]
    match = re.search(r'const visionKeys = \[(.*?)\];', body, re.S)
    assert match, "could not find aiVision's whitelist array in runtime.js"
    assert sorted(re.findall(r'"([^"]+)"', match.group(1))) == sorted(ai_runtime._VISION_OPTIONS)
    assert ai_runtime._VISION_SERVER_OPTIONS == ai_runtime._VISION_OPTIONS | {"base"}
    assert "body.base = ownPath" in body
    assert 'vision: aiVision' in source


@pytest.fixture()
def fused_ai_module():
    if _SHARED_DIR not in sys.path:
        sys.path.insert(0, _SHARED_DIR)
    import fused_ai
    return fused_ai


def test_the_clients_vision_wire_keys_match_the_servers_constant(fused_ai_module):
    assert fused_ai_module._VISION_WIRE_KEYS == ai_runtime._VISION_OPTIONS
    assert callable(fused_ai_module.ai.vision)


def test_the_python_client_sends_an_absolute_image(fused_ai_module, monkeypatch, tmp_path):
    sent = []

    def fake_post(path, body, timeout=None):
        sent.append((path, body))
        return {"ok": True, "result": {"task": "detect", "objects": []}}

    monkeypatch.setattr(fused_ai_module, "_post_json", fake_post)
    monkeypatch.chdir(tmp_path)
    result = fused_ai_module.vision("a.jpg", "detect", "cat", max_objects=2)
    assert result == {"task": "detect", "objects": []}
    assert sent == [("/api/ai/vision", {"image": str(tmp_path / "a.jpg"), "task": "detect",
                                        "prompt": "cat", "maxObjects": 2})]


def test_the_python_client_raises_the_servers_error(fused_ai_module, monkeypatch):
    monkeypatch.setattr(fused_ai_module, "_post_json", lambda p, b, timeout=None: {
        "ok": False, "error": {"type": "model_loading", "message": "loading", "jobId": "j1"}})
    with pytest.raises(fused_ai_module.AiError) as caught:
        fused_ai_module.vision("/a.jpg", "caption")
    assert caught.value.type == "model_loading"


# -- the worker, without torch --------------------------------------------------


class _FakeTorch(types.ModuleType):
    """Only what `generate` touches: `inference_mode`, observable through `inside`."""

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
        self.state = {}

    def download_snapshot(self, model_id, allow_patterns=None, ignore_patterns=None, **kwargs):
        self.downloads.append((model_id, allow_patterns, kwargs))
        return f"/snap/{model_id}/main"

    def download_file(self, repo_id, filename, detail=None, job=None, row=None):
        self.downloads.append(("file", repo_id, filename))
        return f"/snap/{repo_id}/main/{filename}"

    def set_state(self, **fields):
        self.state.update(fields)

    def serve(self, **kwargs):
        pass


@pytest.fixture()
def worker(monkeypatch):
    base = _FakeBase()
    monkeypatch.setitem(sys.modules, "worker_base", base)
    monkeypatch.setitem(sys.modules, "formats", formats)
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch())
    spec = importlib.util.spec_from_file_location("vision_torch_worker_under_test", _WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._base = base
    return module


def test_the_worker_imports_no_model_library_at_import_time(worker):
    assert "torch" not in vars(worker) and "transformers" not in vars(worker)


def test_the_worker_refuses_a_repo_it_has_no_recipe_for(worker):
    with pytest.raises(ValueError, match="not a vision model this runner knows"):
        worker.download("someone/moondream-ft")


def test_moondream_downloads_its_tokenizer_file_then_its_snapshot(worker):
    path = worker.download(MOONDREAM)
    assert worker._base.downloads == [
        ("file", "moondream/starmie-v1", "tokenizer.json"),
        (MOONDREAM, formats.VISION_TORCH_RECIPES[MOONDREAM]["allow"], {}),
    ]
    assert path == f"/snap/{MOONDREAM}/main"


def test_grounding_dino_takes_the_ordinary_download(worker):
    worker.download(DINO)
    assert worker._base.downloads == [(DINO, ["*.json", "model.safetensors"], {})]


@pytest.mark.parametrize("body, needle", [
    ({"task": "look", "image": "/a.jpg"}, "'task'"),
    ({"task": "caption", "image": "a.jpg"}, "absolute"),
    ({"task": "caption", "image": "/a.jpg", "prompt": "x"}, "no 'prompt'"),
    ({"task": "point", "image": "/a.jpg"}, "needs a 'prompt'"),
    ({"task": "detect", "image": "/a.jpg", "prompt": "."}, "needs a 'prompt'"),
    ({"task": "caption", "image": "/a.jpg", "length": "tiny"}, "'length'"),
    ({"task": "detect", "image": "/a.jpg", "prompt": "c", "maxObjects": -1}, "'maxObjects'"),
])
def test_the_worker_validates_its_own_body(worker, body, needle):
    with pytest.raises(ValueError, match=needle):
        worker.validate_request(body)


class _FakeMoondreamModel:
    def caption(self, image, length):
        return {"caption": f"a {length} caption"}

    def query(self, image, question, reasoning):
        return {"answer": "two", "reasoning": {"text": "I count two", "grounding": []}}

    def detect(self, image, obj, settings):
        assert settings == {"max_objects": 50}
        return {"objects": [{"x_min": -0.01, "y_min": 0.2, "x_max": 1.0001, "y_max": 0.5}]}

    def point(self, image, obj, settings):
        assert settings == {"max_objects": 1}
        return {"points": [{"x": 0.25, "y": 0.75}, {"x": 0.5, "y": 0.5}]}


def _moondream(worker):
    family = worker._Moondream.__new__(worker._Moondream)
    family.model = _FakeMoondreamModel()
    family.device = "mps"
    family._encode = lambda path: ("encoded", (640, 480))
    worker._loaded.update(family=family, model_id=MOONDREAM,
                          tasks=formats.VISION_TORCH_RECIPES[MOONDREAM]["tasks"])
    return family


def test_moondream_replies_are_normalized(worker):
    _moondream(worker)
    detect = worker.generate({"task": "detect", "image": "/a.jpg", "prompt": "cat"})
    assert detect["objects"] == [{"xMin": 0.0, "yMin": 0.2, "xMax": 1.0, "yMax": 0.5,
                                  "label": "cat", "score": None}]
    assert (detect["width"], detect["height"], detect["task"]) == (640, 480, "detect")
    query = worker.generate({"task": "query", "image": "/a.jpg", "prompt": "how many?",
                             "reasoning": True})
    assert (query["text"], query["reasoning"]) == ("two", "I count two")
    points = worker.generate({"task": "point", "image": "/a.jpg", "prompt": "cat",
                              "maxObjects": 1})
    assert points["points"] == [{"x": 0.25, "y": 0.75, "label": "cat"}]
    caption = worker.generate({"task": "caption", "image": "/a.jpg", "length": "short"})
    assert caption["text"] == "a short caption"


def test_the_worker_refuses_a_task_its_model_does_not_answer(worker):
    worker._loaded.update(family=object(), model_id=DINO, tasks=("detect",))
    with pytest.raises(ValueError, match="cannot caption"):
        worker.generate({"task": "caption", "image": "/a.jpg"})


def test_a_request_runs_without_autograd(worker):
    # Grad mode is per thread; set at load it never reached the request thread.
    family = _moondream(worker)
    torch = sys.modules["torch"]
    seen = []
    family.run = lambda *a: seen.append(torch.inside) or {"width": 1, "height": 1}
    worker.generate({"task": "caption", "image": "/a.jpg"})
    assert seen == [True]


def test_the_worker_opens_a_rotated_photo_upright(worker, tmp_path):
    from PIL import Image

    path = tmp_path / "phone.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° clockwise to display
    Image.new("RGB", (40, 20)).save(path, exif=exif)
    assert worker._open(str(path)).size == (20, 40)
    with pytest.raises(ValueError, match="no such file"):
        worker._open(str(tmp_path / "gone.jpg"))
