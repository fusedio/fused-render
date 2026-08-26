"""Images attached to scheduled tasks (schedule.shots_dir + /api/schedule/shot).

The shape: the New task form uploads each image once (POST /api/schedule/shot,
bytes under ~/.fused-render/task-shots), schedules with the returned PATHS, and
the fired run gets the paths in its message plus a pre-allowed Read of the dir
(claude_spawn extra_read_dirs -> agent._start). FUSED_RENDER_HOME is redirected
to a tmp dir so nothing touches the real home.
"""
import base64
import json
import os

from fastapi.testclient import TestClient

from fused_render import claude_spawn, schedule
from fused_render.server import create_app


FUSED = {"X-Fused": "1"}  # D3 guard header required on writes

#: A real 1x1 PNG, so the endpoint's happy path stores actual image bytes.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBg"
    "AAAABQABh6FO1AAAAABJRU5ErkJggg=="
)


def _client(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    app = create_app(start_dir=str(tmp_path))
    return TestClient(app), home


def _data_url(raw: bytes = PNG, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


# ---- the upload endpoint -------------------------------------------------------


def test_upload_requires_the_fused_header(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    assert client.post("/api/schedule/shot",
                       json={"data": _data_url()}).status_code == 403


def test_upload_stores_under_task_shots_and_returns_the_path(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    resp = client.post("/api/schedule/shot", json={"data": _data_url()},
                       headers=FUSED)
    assert resp.status_code == 200
    path = resp.json()["path"]
    assert path.startswith(os.path.join(str(home), "task-shots").replace("\\", "/"))
    assert path.endswith(".png")
    with open(path, "rb") as fh:
        assert fh.read() == PNG


def test_upload_refuses_what_is_not_a_base64_image(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    for bad in ("plain text",
                "data:text/html;base64," + base64.b64encode(b"<x>").decode(),
                "data:image/png;base64,***not-base64***",
                "data:image/png;base64,"):
        resp = client.post("/api/schedule/shot", json={"data": bad}, headers=FUSED)
        assert resp.status_code == 400, bad


def test_upload_caps_the_decoded_size(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    from fused_render.server.routers import schedule as schedule_router
    monkeypatch.setattr(schedule_router, "_SHOT_MAX_BYTES", 16)
    resp = client.post("/api/schedule/shot", json={"data": _data_url(b"x" * 17)},
                       headers=FUSED)
    assert resp.status_code == 400
    assert "too large" in resp.json()["error"]


# ---- scheduling with images ----------------------------------------------------


def _upload(client) -> str:
    return client.post("/api/schedule/shot", json={"data": _data_url()},
                       headers=FUSED).json()["path"]


def test_create_stores_validated_paths_and_serves_them_back(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    path = _upload(client)
    resp = client.post("/api/schedule", headers=FUSED, json={
        "target": str(tmp_path), "message": "look at this",
        "delay_seconds": 3600, "images": [path]})
    assert resp.status_code == 200
    entry = resp.json()["entry"]
    assert entry["images"] == [path]
    listed = client.get("/api/schedule").json()["entries"]
    assert [e for e in listed if e["id"] == entry["id"]][0]["images"] == [path]


def test_create_refuses_paths_outside_the_task_shots_dir(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    outside = tmp_path / "not-a-shot.png"
    outside.write_bytes(PNG)
    resp = client.post("/api/schedule", headers=FUSED, json={
        "target": str(tmp_path), "message": "m",
        "delay_seconds": 3600, "images": [str(outside)]})
    assert resp.status_code == 400
    assert "not a task attachment" in resp.json()["error"]


def test_create_refuses_a_symlink_smuggled_into_the_dir(tmp_path, monkeypatch):
    # Realpath membership, not string prefix: a link under the dir pointing out
    # of it must not turn `images` into a way to read arbitrary files.
    client, home = _client(tmp_path, monkeypatch)
    _upload(client)  # ensures the dir exists
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    link = os.path.join(str(home), "task-shots", "link.png")
    os.symlink(str(secret), link)
    resp = client.post("/api/schedule", headers=FUSED, json={
        "target": str(tmp_path), "message": "m",
        "delay_seconds": 3600, "images": [link]})
    assert resp.status_code == 400


def test_create_caps_the_image_count(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    paths = [_upload(client) for _ in range(schedule.IMAGES_MAX + 1)]
    resp = client.post("/api/schedule", headers=FUSED, json={
        "target": str(tmp_path), "message": "m",
        "delay_seconds": 3600, "images": paths})
    assert resp.status_code == 400
    assert "at most" in resp.json()["error"]


def test_a_recurring_occurrence_carries_the_templates_images(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    path = _upload(client)
    resp = client.post("/api/schedule", headers=FUSED, json={
        "target": str(tmp_path), "message": "m", "repeats": "0 9 * * *",
        "images": [path]})
    assert resp.status_code == 200
    entries = client.get("/api/schedule").json()["entries"]
    occurrence = [e for e in entries if e.get("template_id")]
    assert occurrence and occurrence[0]["images"] == [path]


# ---- what the fired run is handed ----------------------------------------------


def test_attachments_block_names_the_paths_for_the_read_tool(tmp_path, monkeypatch):
    entry = {"images": ["/x/task-shots/a.png", "/x/task-shots/b.png"]}
    block = schedule._attachments_block(entry)
    assert "read them with the Read tool" in block
    assert block.endswith("/x/task-shots/a.png\n/x/task-shots/b.png")
    assert schedule._attachments_block({}) == ""
    assert schedule._attachments_block({"images": []}) == ""


def test_spawn_helper_ships_extra_read_dirs_to_the_agent(monkeypatch):
    seen = {}

    class _Res:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(cmd, *, input, **kw):
        seen.update(json.loads(input))
        return _Res()

    monkeypatch.setattr(claude_spawn.subprocess, "run", fake_run)
    claude_spawn.spawn_helper("/tmp/t", "hi", "auto",
                              extra_read_dirs=["/x/task-shots"])
    assert seen["extra_read_dirs"] == ["/x/task-shots"]
    claude_spawn.spawn_helper("/tmp/t", "hi", "auto")
    assert seen["extra_read_dirs"] == []


def test_a_send_without_images_keeps_the_old_call_shape(tmp_path, monkeypatch):
    """No images, no `extra_read_dirs` kwarg — not even as None.

    Regression: passing it unconditionally broke every test double in the repo
    (`fake_spawn(target, prompt, permission_mode, session_id="")`), and because
    `_send` catches a bad spawn into `_fail`, the symptom was a `failed` event
    on a task that had nothing wrong with it. A run with nothing to read there
    should also not carry a directory rule.
    """
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    seen = {}

    def spy(target, prompt, permission_mode, session_id="", **kw):
        seen["kw"] = kw
        return {"run_id": "r-1"}

    monkeypatch.setattr(schedule.claude_spawn, "spawn_helper", spy)
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    monkeypatch.setattr(schedule, "_report", lambda *a, **k: None)
    monkeypatch.setattr(schedule, "_watching", lambda *a, **k: None)
    monkeypatch.setattr(schedule, "_update", lambda *a, **k: None)
    schedule._send({"id": "x", "target": str(tmp_path), "message": "plain",
                    "session_id": "", "permission_mode": "auto"})
    assert "extra_read_dirs" not in seen["kw"]


def test_a_send_with_images_pre_allows_the_task_shots_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    seen = {}

    def spy(target, prompt, permission_mode, session_id="", **kw):
        seen["kw"] = kw
        seen["prompt"] = prompt
        return {"run_id": "r-1"}

    monkeypatch.setattr(schedule.claude_spawn, "spawn_helper", spy)
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    monkeypatch.setattr(schedule, "_report", lambda *a, **k: None)
    monkeypatch.setattr(schedule, "_watching", lambda *a, **k: None)
    monkeypatch.setattr(schedule, "_update", lambda *a, **k: None)
    shot = os.path.join(schedule.shots_dir(), "a.png")
    schedule._send({"id": "x", "target": str(tmp_path), "message": "look",
                    "session_id": "", "permission_mode": "auto",
                    "images": [shot]})
    assert seen["kw"]["extra_read_dirs"] == [schedule.shots_dir()]
    assert shot in seen["prompt"]


def test_the_pre_allowed_dir_and_the_stored_paths_have_ONE_spelling(tmp_path, monkeypatch):
    """The Read rule matches TEXT, so both sides must resolve identically.

    A symlink anywhere on the path — a symlinked home, macOS' own
    /tmp -> /private/tmp — used to leave `shots_dir()` unresolved while
    `_images` stored realpaths, and the headless run was handed paths its rule
    did not cover (Bugbot, PR #865).
    """
    real = tmp_path / "real-home"
    real.mkdir()
    link = tmp_path / "linked-home"
    os.symlink(str(real), str(link))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(link))
    assert schedule.shots_dir() == os.path.realpath(schedule.shots_dir())

    client = TestClient(create_app(start_dir=str(tmp_path)))
    path = client.post("/api/schedule/shot", json={"data": _data_url()},
                       headers=FUSED).json()["path"]
    # The upload's own answer, the validator's, and the pre-allowed dir all
    # agree — which is the whole property.
    assert path.startswith(schedule.shots_dir())
    resp = client.post("/api/schedule", headers=FUSED, json={
        "target": str(tmp_path), "message": "m",
        "delay_seconds": 3600, "images": [path]})
    assert resp.status_code == 200, resp.json()
    assert resp.json()["entry"]["images"] == [path]


def test_agent_start_turns_extra_dirs_into_read_rules():
    # A source pin, because _start launches a real process: the helper's
    # request field must land in the run's --allowed-tools as a Read rule,
    # exactly the SHOTS mechanism (agent._read_rule).
    src = open(os.path.join(os.path.dirname(__file__), "..", "fused_render",
                            "templates", "claude", "agent.py"),
                encoding="utf-8").read()
    assert "extra_read_dirs: list | None = None" in src
    assert "[_read_rule(d) for d in (extra_read_dirs or [])]" in src
