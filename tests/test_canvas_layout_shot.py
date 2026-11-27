"""A picture of a canvas's layout with no canvas page open (SPEC §28 CV-8,
D1512): `canvas_layout_shot` (one headless browser per picture, driven over
DevTools until the page says it is drawn), the route that serves it
(`POST /api/canvases/layout-shot`) and the canvas view's picture mode.

No real browser is started here: the launch and the DevTools conversation
are stood in for, so what is pinned is the contract around them (what is
asked of the browser, when the picture is taken, every way it can fail).
"""
import base64
import os

import pytest
from fastapi.testclient import TestClient

from fused_render import canvas_layout_shot as shot_mod
from fused_render.server import create_app
from fused_render.server import templates as server_templates

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")

CANVAS = """\
type = "canvas"
version = 2

[canvas]
edges = [["load", "chart"]]

[[canvas.nodes]]
udfName = "Inputs"
type = "udf-folder"
folderName = "Inputs"
childUdfOrder = ["load"]
x = 0
y = 0
width = 780
height = 615

[[canvas.nodes]]
udfName = "load"
x = 40
y = 75

[[canvas.nodes]]
udfName = "chart"
x = 900
y = 75
"""

@pytest.fixture
def canvas(tmp_path):
    folder = tmp_path / "demo"
    folder.mkdir()
    (folder / "canvas.toml").write_text(CANVAS, encoding="utf-8")
    return folder


# ------------------------------------------------------------------ the module

def test_names_are_every_row_and_folder_name_once():
    doc = {"canvas": {"nodes": [
        {"udfName": "Inputs", "folderName": "Inputs", "type": "udf-folder"},
        {"udfName": "grp", "folderName": "Group label", "type": "udf-folder"},
        {"udfName": "load"}, {"udfName": "load"}, "junk", {"x": 1},
    ]}}
    assert shot_mod.names_in(doc) == ["Inputs", "grp", "Group label", "load"]
    assert shot_mod.names_in({}) == [] and shot_mod.names_in({"canvas": 3}) == []


def test_a_canvas_that_cannot_be_read_is_a_sentence(tmp_path, canvas):
    assert "no canvas.toml" in shot_mod.read_canvas(str(tmp_path / "nope" / "canvas.toml"))["error"]
    bad = tmp_path / "bad.toml"
    bad.write_text("type = [", encoding="utf-8")
    assert "does not parse" in shot_mod.read_canvas(str(bad))["error"]
    plain = tmp_path / "plain.toml"
    plain.write_text('name = "x"\n', encoding="utf-8")
    assert "not a canvas" in shot_mod.read_canvas(str(plain))["error"]
    assert "doc" in shot_mod.read_canvas(str(canvas / "canvas.toml"))


def test_a_folder_name_counts_on_a_folder_row_only():
    # The view knows a plain node by its udfName alone: a stray `folderName`
    # on one must not pass as something a picture can be framed on.
    doc = {"canvas": {"nodes": [{"udfName": "actual", "folderName": "extra"}]}}
    assert shot_mod.names_in(doc) == ["actual"]


def test_text_that_is_not_utf8_does_not_parse(tmp_path):
    bad = tmp_path / "canvas.toml"
    bad.write_bytes(b'type = "canvas"\nname = "\xff\xfe"\n')
    assert "does not parse" in shot_mod.read_canvas(str(bad))["error"]


def test_a_node_list_that_is_no_list_is_an_empty_canvas(tmp_path, monkeypatch):
    path = tmp_path / "canvas.toml"
    path.write_text('type = "canvas"\n[canvas]\nnodes = 1\nedges = 2\n')
    monkeypatch.setattr(shot_mod, "_find_chrome", lambda: "/bin/chrome")
    monkeypatch.setattr(shot_mod, "_render", lambda chrome, url, deadline: PNG)
    out = shot_mod.shot(str(path), "http://h", "/t.html")
    assert (out["nodes"], out["folders"], out["edges"]) == (0, 0, 0)


def test_the_wait_for_a_slot_is_inside_the_budget(canvas, monkeypatch):
    # Both slots taken: the caller is told so instead of queueing past its
    # own caller's patience, and a slot is not leaked either way.
    monkeypatch.setattr(shot_mod, "_find_chrome", lambda: "/bin/chrome")
    monkeypatch.setattr(shot_mod, "_SLOTS", shot_mod.threading.BoundedSemaphore(1))
    monkeypatch.setattr(shot_mod, "MIN_DRAW_S", 0.0)
    seen = []
    monkeypatch.setattr(shot_mod, "_render",
                        lambda chrome, url, deadline: seen.append(deadline) or PNG)
    assert shot_mod._SLOTS.acquire()
    started = shot_mod.time.monotonic()
    out = shot_mod.shot(str(canvas / "canvas.toml"), "http://h", "/t.html", timeout=0.3)
    assert "try again" in out["error"] and seen == []
    assert shot_mod.time.monotonic() - started < 2
    shot_mod._SLOTS.release()
    # Free again: the picture is drawn, against a deadline set BEFORE the wait.
    before = shot_mod.time.monotonic()
    assert shot_mod.shot(str(canvas / "canvas.toml"), "http://h", "/t.html", timeout=5)["png"] == PNG
    assert before + 4.5 < seen[0] <= shot_mod.time.monotonic() + 5
    assert shot_mod._SLOTS.acquire(blocking=False)      # released after the draw
    shot_mod._SLOTS.release()


def test_the_page_asked_for_is_the_canvas_view_in_picture_mode():
    url = shot_mod.view_url("http://127.0.0.1:1777/", "/t/canvas/template.html",
                            "/c/My canvas/canvas.toml", focus="A & B")
    assert url.startswith("http://127.0.0.1:1777/render?")
    assert "path=%2Ft%2Fcanvas%2Ftemplate.html" in url
    assert "_file=%2Fc%2FMy+canvas%2Fcanvas.toml" in url
    assert "shot=1" in url and "focus=A+%26+B" in url
    assert "focus" not in shot_mod.view_url("http://h", "/t", "/c/canvas.toml")


def test_the_browser_is_headless_with_a_profile_of_its_own():
    argv = shot_mod.chrome_argv("/bin/chrome", "/w/profile")
    assert argv[0] == "/bin/chrome" and argv[-1] == "about:blank"
    assert "--headless=new" in argv
    assert "--user-data-dir=/w/profile" in argv        # never the user's own profile
    assert "--remote-debugging-port=0" in argv         # Chrome picks; read from the profile
    assert "--window-size=%d,%d" % shot_mod.SIZE in argv
    assert not any(a.startswith("--screenshot") for a in argv)


def test_the_browser_is_spawned_without_a_fork(monkeypatch, tmp_path):
    # `close_fds=False` and no new session are what let CPython posix_spawn:
    # a fork() of the server dies before exec once libproj is loaded.
    seen = {}

    class Proc:
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            seen["terminated"] = True

        def wait(self, timeout=None):
            return 0

    def popen(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return Proc()

    monkeypatch.setattr(shot_mod.subprocess, "Popen", popen)
    monkeypatch.setattr(shot_mod, "_debug_port", lambda profile, proc, deadline: 4242)
    monkeypatch.setattr(shot_mod, "_capture", lambda port, url, deadline: PNG if port == 4242 else b"")
    monkeypatch.setattr(shot_mod.tempfile, "mkdtemp", lambda prefix="": str(_made(tmp_path / "work")))
    assert shot_mod._render("/bin/chrome", "http://h/render?x", 5) == PNG
    assert seen["kwargs"]["close_fds"] is False
    assert "start_new_session" not in seen["kwargs"] and "preexec_fn" not in seen["kwargs"]
    assert seen["argv"][0] == "/bin/chrome"
    # The browser is ended and its folder removed, picture or not.
    assert seen["terminated"] is True
    assert not (tmp_path / "work").exists()


def _made(path):
    path.mkdir()
    return path


def test_the_debug_port_is_read_from_the_profile(tmp_path):
    class Alive:
        returncode = None

        def poll(self):
            return None

    (tmp_path / "DevToolsActivePort").write_text("51234\n/devtools/browser/abc\n")
    assert shot_mod._debug_port(str(tmp_path), Alive(), shot_mod.time.monotonic() + 2) == 51234

    class Dead:
        returncode = -11

        def poll(self):
            return -11

    with pytest.raises(shot_mod._Failed, match="exited at launch"):
        shot_mod._debug_port(str(tmp_path / "none"), Dead(), shot_mod.time.monotonic() + 2)
    with pytest.raises(shot_mod._Failed, match="did not come up"):
        shot_mod._debug_port(str(tmp_path / "none"), Alive(), shot_mod.time.monotonic() + 0.3)


class _FakeWS:
    """The DevTools side of one page: `states` is what the page's root
    attribute reads on each poll."""

    def __init__(self, states, data=None):
        self.states = list(states)
        self.data = base64.b64encode(PNG).decode("ascii") if data is None else data
        self.calls = []
        self.closed = False

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "Runtime.evaluate" and "getAttribute" in params.get("expression", ""):
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return {"result": {"value": state}}
        if method == "Page.captureScreenshot":
            return {"data": self.data}
        return {}

    def close(self):
        self.closed = True


def _with_ws(monkeypatch, ws):
    from fused_render.bots import browser

    monkeypatch.setattr(shot_mod, "_page_socket", lambda port, deadline: "ws://127.0.0.1:1/x")
    monkeypatch.setattr(browser, "WS", lambda url, timeout=30: ws)
    monkeypatch.setattr(shot_mod, "_POLL_S", 0.01)


def test_the_picture_is_taken_when_the_page_says_it_is_drawn(monkeypatch):
    ws = _FakeWS([None, None, "ready"])
    _with_ws(monkeypatch, ws)
    png = shot_mod._capture(1, "http://h/render?x", shot_mod.time.monotonic() + 5)
    assert png == PNG and ws.closed
    methods = [m for m, _ in ws.calls]
    assert methods[:2] == ["Page.enable", "Page.navigate"]
    assert ws.calls[1][1] == {"url": "http://h/render?x"}
    # Asked three times for the page's word, and photographed only after it.
    polls = [i for i, (m, p) in enumerate(ws.calls)
             if m == "Runtime.evaluate" and "data-cv" in p["expression"]]
    assert len(polls) == 3
    assert methods.index("Page.captureScreenshot") > polls[-1]
    assert methods[-1] == "Page.captureScreenshot"


def test_a_page_that_fails_or_never_finishes_is_a_sentence(monkeypatch):
    ws = _FakeWS(["failed"])
    _with_ws(monkeypatch, ws)
    with pytest.raises(shot_mod._Failed, match="could not read this canvas"):
        shot_mod._capture(1, "http://h", shot_mod.time.monotonic() + 5)
    assert ws.closed and "Page.captureScreenshot" not in [m for m, _ in ws.calls]

    ws = _FakeWS([None])
    _with_ws(monkeypatch, ws)
    with pytest.raises(shot_mod._Failed, match="took too long"):
        shot_mod._capture(1, "http://h", shot_mod.time.monotonic() + 0.2)

    ws = _FakeWS(["ready"], data="bm90IGEgcG5n")   # "not a png"
    _with_ws(monkeypatch, ws)
    with pytest.raises(shot_mod._Failed, match="did not hand back a picture"):
        shot_mod._capture(1, "http://h", shot_mod.time.monotonic() + 5)

    class Broken(_FakeWS):
        def call(self, method, **params):
            raise RuntimeError("websocket closed")
    ws = Broken(["ready"])
    _with_ws(monkeypatch, ws)
    with pytest.raises(shot_mod._Failed, match="stopped answering"):
        shot_mod._capture(1, "http://h", shot_mod.time.monotonic() + 5)
    assert ws.closed


def test_a_browser_gone_before_its_socket_opens_is_a_sentence(monkeypatch):
    from fused_render.bots import browser

    def refuse(url, timeout=30):
        raise ConnectionRefusedError("refused")
    monkeypatch.setattr(shot_mod, "_page_socket", lambda port, deadline: "ws://127.0.0.1:1/x")
    monkeypatch.setattr(browser, "WS", refuse)
    with pytest.raises(shot_mod._Failed, match="could not be reached"):
        shot_mod._capture(1, "http://h", shot_mod.time.monotonic() + 5)


def test_no_browser_installed_is_a_sentence_and_a_flag(canvas, monkeypatch):
    monkeypatch.setattr(shot_mod, "_find_chrome", lambda: None)
    out = shot_mod.shot(str(canvas / "canvas.toml"), "http://h", "/t.html")
    assert out["no_browser"] is True and "browser" in out["error"]


def test_an_unknown_focus_lists_what_can_be_named(canvas, monkeypatch):
    # Refused before any browser is looked for.
    monkeypatch.setattr(shot_mod, "_find_chrome", lambda: pytest.fail("no browser needed"))
    out = shot_mod.shot(str(canvas / "canvas.toml"), "http://h", "/t.html", focus="nope")
    assert 'named "nope"' in out["error"]
    assert "Inputs, load, chart" in out["error"]


def test_a_picture_comes_back_with_the_canvas_counts(canvas, monkeypatch):
    seen = []
    monkeypatch.setattr(shot_mod, "_find_chrome", lambda: "/bin/chrome")
    monkeypatch.setattr(shot_mod, "_render",
                        lambda chrome, url, timeout: seen.append((chrome, url)) or PNG)
    out = shot_mod.shot(str(canvas / "canvas.toml"), "http://127.0.0.1:9", "/t/template.html",
                        focus="Inputs")
    assert out["png"] == PNG
    assert (out["nodes"], out["folders"], out["edges"], out["focus"]) == (2, 1, 1, "Inputs")
    assert (out["width"], out["height"]) == (1, 1)      # read off the PNG itself
    chrome, url = seen[0]
    assert chrome == "/bin/chrome"
    assert url.startswith("http://127.0.0.1:9/render?") and "focus=Inputs" in url and "shot=1" in url


def test_a_browser_failure_is_the_sentence_it_raised(canvas, monkeypatch):
    def boom(chrome, url, timeout):
        raise shot_mod._Failed("the browser did not come up")
    monkeypatch.setattr(shot_mod, "_find_chrome", lambda: "/bin/chrome")
    monkeypatch.setattr(shot_mod, "_render", boom)
    assert shot_mod.shot(str(canvas / "canvas.toml"), "http://h", "/t.html") == {
        "error": "the browser did not come up"}


# ------------------------------------------------------------------- the route

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    return TestClient(create_app(start_dir=str(tmp_path)))


def test_the_route_needs_the_guard_header(client, canvas):
    res = client.post("/api/canvases/layout-shot", json={"path": str(canvas)})
    assert res.status_code in (400, 403)


def test_the_route_hands_back_the_picture(client, canvas, monkeypatch):
    calls = []

    def fake(toml_path, origin, template, focus=""):
        calls.append((toml_path, origin, template, focus))
        return {"png": PNG, "width": 10, "height": 5, "nodes": 2, "folders": 1,
                "edges": 1, "focus": focus}
    monkeypatch.setattr(shot_mod, "shot", fake)
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:4321")
    res = client.post("/api/canvases/layout-shot", headers={"X-Fused": "1"},
                      json={"path": str(canvas), "focus": " Inputs "})
    assert res.status_code == 200
    body = res.json()
    assert base64.b64decode(body["image"]) == PNG and body["mime"] == "image/png"
    assert (body["nodes"], body["folders"], body["edges"], body["focus"]) == (2, 1, 1, "Inputs")
    assert "png" not in body
    toml_path, origin, template, focus = calls[0]
    # A folder means its canvas.toml; the page is the shipped canvas view.
    assert toml_path == str(canvas / "canvas.toml")
    assert origin == "http://127.0.0.1:4321"
    assert template == os.path.join(server_templates.TEMPLATES_DIR, "canvas", "template.html")
    assert focus == "Inputs"
    # The file itself is taken as given.
    client.post("/api/canvases/layout-shot", headers={"X-Fused": "1"},
                json={"path": str(canvas / "canvas.toml")})
    assert calls[1][0] == str(canvas / "canvas.toml")


def test_the_route_answers_422_with_the_sentence(client, canvas, monkeypatch):
    monkeypatch.setattr(shot_mod, "shot", lambda *a, **k: {"error": "no browser", "no_browser": True})
    res = client.post("/api/canvases/layout-shot", headers={"X-Fused": "1"},
                      json={"path": str(canvas)})
    assert res.status_code == 422
    assert res.json() == {"error": "no browser", "no_browser": True}
    for body in ({}, {"path": "relative/dir"}, {"path": str(canvas), "focus": 3}):
        assert client.post("/api/canvases/layout-shot", headers={"X-Fused": "1"},
                           json=body).status_code == 400


# -------------------------------------------------------- the view's picture mode

def test_the_canvas_view_has_a_picture_mode():
    with open(os.path.join(server_templates.TEMPLATES_DIR, "canvas", "template.html"),
              encoding="utf-8") as fh:
        html = fh.read()
    # `shot=1`: whole canvas, no controls, whatever camera the URL or file holds.
    assert 'ownParam("shot") === "1"' in html
    assert ".stage.shot .controls { display: none; }" in html
    assert "!shotMode && px0 !== null" in html
    # `focus=<name>` frames one node or folder by udfName or folderName.
    assert 'ownParam("focus")' in html
    assert "n.udfName === focusName || n.folderName === focusName" in html
    # The page says when it is drawn: "failed" until the canvas is built and
    # its typeface is in, then "ready" (what the picture waits for).
    assert 'setAttribute("data-cv", "failed")' in html
    assert 'setAttribute("data-cv", "ready")' in html
    assert shot_mod.READY_ATTR == "data-cv"
    # "failed" is said by a failure only (an unhandled rejection or error),
    # never on the way to "ready": the typeface can take up to two seconds
    # after a good read, and a poll in that window must see nothing yet.
    read = html[html.index('data = await fused.runPython("./reader.py"'):]
    after_read = read[:read.index("data = data || {};")]
    assert "data-cv" not in after_read
    assert 'window.addEventListener("unhandledrejection", markFailed);' in html
    assert html.count('setAttribute("data-cv", "failed")') == 1
    # A frame holds all of what it names: no lower zoom limit of the
    # interactive range, the descriptions under its nodes measured in, and a
    # note's pictures waited for (and never lazy).
    assert "Math.max(FRAME_MIN_ZOOM, Math.min(iw / bw, ih / bh, 1))" in html
    assert "camForFrame(withDescriptions(fb))" in html and "camForFrame(withDescriptions(ab))" in html
    assert 'e.setAttribute("loading", shotMode ? "eager" : "lazy");' in html
    assert 'world.querySelectorAll("img")' in html
