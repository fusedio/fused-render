"""bots/browser.py: the pure helpers (no Chrome), plus one opt-in integration
test against a real headless Chrome (BOTS_CHROME_TESTS=1)."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import json
import os
import threading
import urllib.parse

import pytest

from fused_render.bots import browser as br


# ------------------------------------------------------------ _key_params ---
def test_key_params_enter_carries_carriage_return():
    down = br._key_params("Enter", "Enter", 0, True)
    assert down["type"] == "keyDown" and down["windowsVirtualKeyCode"] == 13
    assert down["text"] == down["unmodifiedText"] == "\r"
    up = br._key_params("Enter", "Enter", 0, False)
    assert up["type"] == "keyUp" and "text" not in up


def test_key_params_printable_and_shift():
    p = br._key_params("A", "KeyA", 8, True)
    assert p["text"] == "A" and p["windowsVirtualKeyCode"] == ord("A") and p["modifiers"] == 8


def test_key_params_ctrl_letter_is_a_command_without_text():
    p = br._key_params("a", "KeyA", 2, True)
    assert "text" not in p and p["commands"] == ["SelectAll"]
    p = br._key_params("v", "KeyV", 4, True)
    assert p["commands"] == ["Paste"]
    assert "commands" not in br._key_params("q", "KeyQ", 4, True)


# -------------------------------------------------------------- _find_js ---
def test_find_js_embeds_ref_text_and_point():
    js = br._find_js("", "sb7", 'Say "hi"', 10, 20)
    assert js.startswith("(() => {") and js.rstrip().endswith("})()")
    assert 'const ref = "sb7"' in js
    assert json.dumps('Say "hi"') in js
    assert "const px = 10, py = 20" in js
    assert "data-sb-ref" in js and "elementFromPoint" in js


def test_find_js_strict_drops_the_text_and_point_guesses():
    js = br._find_js("", "sb7", "Reply", 10, 20, strict=True)
    assert 'const ref = "sb7"' in js
    assert 'txt = ""' in js
    assert "const px = null, py = null" in js


def test_with_scheme():
    assert br._with_scheme("example.com") == "https://example.com"
    assert br._with_scheme("http://x.test/a") == "http://x.test/a"
    assert br._with_scheme("data:text/html,<p>hi</p>") == "data:text/html,<p>hi</p>"
    assert br._with_scheme("about:blank") == "about:blank"


# --------------------------------------------------------- ax_candidates ---
def _ax(nid, role, name="", parent=None, kids=(), backend=None, ignored=False, props=None):
    n = {"nodeId": str(nid), "ignored": ignored, "role": {"type": "role", "value": role},
         "childIds": [str(k) for k in kids]}
    if name:
        n["name"] = {"type": "computedString", "value": name}
    if parent is not None:
        n["parentId"] = str(parent)
    if backend is not None:
        n["backendDOMNodeId"] = backend
    if props:
        n["properties"] = [{"name": k, "value": {"type": "x", "value": v}} for k, v in props.items()]
    return n


def test_ax_candidates_order_roles_states_and_dialog():
    nodes = [
        _ax(1, "RootWebArea", "page", kids=(2, 3, 4, 9, 11), backend=1),
        _ax(2, "heading", "Title", parent=1, backend=2),
        _ax(3, "generic", "", parent=1, kids=(5,), backend=3, ignored=True),
        _ax(5, "button", "Inside ignored", parent=3, backend=5, props={"disabled": True}),
        _ax(4, "dialog", "Cookies", parent=1, kids=(6, 7), backend=4),
        _ax(6, "checkbox", "Remember", parent=4, backend=6, props={"checked": "true", "focused": True}),
        _ax(7, "button", "Accept", parent=4, backend=7, props={"expanded": False}),
        _ax(9, "heading", "", parent=1, backend=9),           # unnamed heading: dropped
        _ax(11, "img", "Logo", parent=1, kids=(12,), backend=11),
        _ax(12, "StaticText", "Logo", parent=11, backend=12),  # not a kept role
    ]
    out = br.ax_candidates(nodes)
    assert [c["text"] for c in out] == ["Title", "Inside ignored", "Cookies", "Remember", "Accept", "Logo"]
    by = {c["text"]: c for c in out}
    assert by["Inside ignored"]["disabled"] is True and "dialog" not in by["Inside ignored"]
    assert by["Cookies"]["role"] == "dialog" and by["Cookies"]["dialog"] is True
    assert by["Remember"]["checked"] is True and by["Remember"]["focused"] is True and by["Remember"]["dialog"]
    assert by["Accept"]["expanded"] is False and "checked" not in by["Accept"]
    assert by["Logo"]["role"] == "image"


def test_ax_candidates_one_per_backend_node_and_aliases():
    nodes = [
        _ax(1, "RootWebArea", kids=(2, 3, 4)),
        _ax(2, "PopUpButton", "Colour", parent=1, backend=20),
        _ax(3, "combobox", "Colour again", parent=1, backend=20),
        _ax(4, "MenuListOption", "Red", parent=1, backend=21, props={"selected": True}),
    ]
    out = br.ax_candidates(nodes)
    assert [(c["role"], c["backend"]) for c in out] == [("combobox", 20), ("option", 21)]
    assert out[1]["selected"] is True


# --------------------------------------------------------- merge_elements ---
VP = (1280, 713)


def test_merge_same_stamp_keeps_dom_dict_and_adds_ax_states():
    ax = [{"ref": "sb1", "role": "combobox", "tag": "select", "text": "Colour", "x": 100, "y": 50,
           "backend": 41, "expanded": False, "focused": True}]
    dom = [{"ref": "sb1", "tag": "select", "text": "Red Green", "x": 100, "y": 50, "type": "select-one",
            "options": ["Red", "Green"], "value": "Red", "name": "colour"}]
    (e,) = br.merge_elements(ax, dom, VP)
    assert e["ref"] == "sb1" and e["backend"] == 41
    assert e["options"] == ["Red", "Green"] and e["value"] == "Red" and e["name"] == "colour"
    assert e["text"] == "Colour"  # a field reads as its label; the value stays in `value`
    assert e["focused"] is True
    assert "expanded" not in e  # every native <select> reads collapsed; not worth a word
    assert "role" not in e  # combobox is implied by <select>
    ax[0].update(tag="div", role="combobox")
    dom[0].update(tag="div")
    (e,) = br.merge_elements(ax, dom, VP)
    assert e["expanded"] is False and e["role"] == "combobox"  # a custom combobox keeps it


def test_merge_same_stamp_button_keeps_dom_text():
    ax = [{"ref": "sb2", "role": "button", "tag": "button", "text": "Close dialog", "x": 5, "y": 5, "backend": 7}]
    dom = [{"ref": "sb2", "tag": "button", "text": "Close", "x": 5, "y": 5}]
    (e,) = br.merge_elements(ax, dom, VP)
    assert e["text"] == "Close"  # what _find_js's text fallback matches
    ax[0]["text"] = "Settings"
    dom[0]["text"] = ""
    (e,) = br.merge_elements(ax, dom, VP)
    assert e["text"] == "Settings"  # icon button: the AX name fills the gap


def test_merge_dedupes_by_text_and_proximity():
    ax = [{"ref": "sb5", "role": "button", "tag": "button", "text": "Upload", "x": 200, "y": 300, "backend": 9}]
    dom = [{"ref": "sb2", "tag": "label", "text": "Upload", "x": 203, "y": 298, "upload": True, "accept": "image/*"}]
    (e,) = br.merge_elements(ax, dom, VP)
    assert e["ref"] == "sb5" and e["backend"] == 9  # the AX node stays the target
    assert e["upload"] is True and e["accept"] == "image/*"


def test_merge_does_not_fold_far_or_heading_records():
    ax = [{"ref": "sb5", "role": "heading", "tag": "h2", "text": "Settings", "x": 100, "y": 100, "backend": 1},
          {"ref": "sb6", "role": "button", "tag": "button", "text": "Save", "x": 100, "y": 400, "backend": 2}]
    dom = [{"ref": "sb1", "tag": "a", "text": "Settings", "x": 100, "y": 100, "href": "https://x/s"},
           {"ref": "sb2", "tag": "div", "text": "Save", "x": 500, "y": 400}]
    out = br.merge_elements(ax, dom, VP)
    assert [e["ref"] for e in out] == ["sb5", "sb1", "sb6", "sb2"]
    assert out[0]["tag"] == "h2" and "role" not in out[0]  # heading is implied by <h2>
    assert out[1]["href"] == "https://x/s" and "backend" not in out[1]


def test_merge_keeps_document_order_for_dom_only_records():
    ax = [{"ref": "sb3", "role": "button", "tag": "button", "text": "A", "x": 10, "y": 10, "backend": 1},
          {"ref": "sb4", "role": "button", "tag": "button", "text": "D", "x": 10, "y": 40, "backend": 2}]
    dom = [{"ref": "sb0", "tag": "a", "text": "first", "x": 10, "y": 5},
           {"ref": "sb3", "tag": "button", "text": "A", "x": 10, "y": 10},
           {"ref": "sb1", "tag": "div", "text": "B", "x": 10, "y": 20},
           {"ref": "sb2", "tag": "div", "text": "C", "x": 10, "y": 30},
           {"ref": "sb4", "tag": "button", "text": "D", "x": 10, "y": 40}]
    out = br.merge_elements(ax, dom, VP)
    assert [e["text"] for e in out] == ["first", "A", "B", "C", "D"]


def test_merge_viewport_first_offscreen_flag_and_cap():
    ax = [{"ref": f"sb{i}", "role": "link", "tag": "a", "text": f"L{i}", "x": 50, "y": 900 if i % 2 else 100,
           "backend": i} for i in range(1, 11)]
    out = br.merge_elements(ax, [], VP)
    assert [e["ref"] for e in out[:5]] == ["sb2", "sb4", "sb6", "sb8", "sb10"]
    assert all(not e.get("offscreen") for e in out[:5])
    assert all(e["offscreen"] is True for e in out[5:])
    assert all("role" not in e for e in out)  # link is implied by <a>
    capped = br.merge_elements(ax, [], VP, cap=3)
    assert [e["ref"] for e in capped] == ["sb2", "sb4", "sb6"]


def test_merge_dom_only_fallback_and_dialog_or():
    dom = [{"ref": "sb1", "tag": "button", "text": "OK", "x": 10, "y": 10, "dialog": True},
           {"ref": "sb2", "tag": "a", "text": "Below", "x": 10, "y": 1200}]
    out = br.merge_elements([], dom, VP)
    assert [e["ref"] for e in out] == ["sb1", "sb2"]
    assert out[0]["dialog"] is True and out[1]["offscreen"] is True
    ax = [{"ref": "sb1", "role": "button", "tag": "button", "text": "OK", "x": 10, "y": 10, "backend": 3}]
    (e, _) = br.merge_elements(ax, dom, VP)
    assert e["dialog"] is True and e["backend"] == 3


def test_merge_drops_ax_records_without_ref():
    ax = [{"role": "button", "text": "ghost", "x": 1, "y": 1, "backend": 1}]
    assert br.merge_elements(ax, [], VP) == []


def test_snapshot_falls_back_to_dom_scan_and_logs_once(tmp_path, monkeypatch, caplog):
    b = br.Browser(str(tmp_path / "data"), str(tmp_path / "cache"))
    scan = {"elements": [{"ref": "sb1", "tag": "button", "text": "Go", "x": 10, "y": 10},
                         {"ref": "sb2", "tag": "a", "text": "Far", "x": 10, "y": 1000}],
            "n": 2, "vw": 1280, "vh": 713}
    monkeypatch.setattr(br.Browser, "_eval", staticmethod(lambda ws, expr: scan))

    def boom(ws, n, vh):
        raise RuntimeError("Accessibility.getFullAXTree: not supported")

    monkeypatch.setattr(b, "_ax_elements", boom)
    with caplog.at_level("WARNING", logger=br.__name__):
        first = b._snapshot(None)
        second = b._snapshot(None)
    assert first == second
    assert [e["ref"] for e in first] == ["sb1", "sb2"]
    assert "offscreen" not in first[0] and first[1]["offscreen"] is True
    assert b._ax_warned is True
    assert len([r for r in caplog.records if "accessibility snapshot failed" in r.getMessage()]) == 1


# ------------------------------------------------------------ shoot_actions ---
class _FakeWS:
    def close(self):
        pass


def test_run_follows_shoot_actions_but_observe_and_screenshot_always_shoot(tmp_path, monkeypatch):
    b = br.Browser(str(tmp_path / "data"), str(tmp_path / "cache"))
    os.makedirs(b.cache_dir, exist_ok=True)
    shots = []
    monkeypatch.setattr(b, "_connect", lambda timeout=30: (_FakeWS(), {}))
    monkeypatch.setattr(br.Browser, "_settle_dialog", staticmethod(lambda ws: None))
    monkeypatch.setattr(br.Browser, "_eval", staticmethod(lambda ws, expr: {"url": "https://a.test", "title": "A"}))
    monkeypatch.setattr(b, "_shoot", lambda ws: shots.append(ws))
    monkeypatch.setattr(b, "_snapshot", lambda ws: [])
    monkeypatch.setattr(b, "tabs", lambda: [])
    monkeypatch.setattr(b, "list_downloads", lambda limit=20: [])
    assert b.shoot_actions is True
    b._run(lambda ws: None)
    assert len(shots) == 1
    b.shoot_actions = False
    out, info = b._run(lambda ws: "x")
    assert out == "x" and info["url"] == "https://a.test"  # url/title still recorded without a shot
    assert len(shots) == 1
    b._run(lambda ws: None, shoot=True)  # an explicit True wins over the attribute
    assert len(shots) == 2
    b.observe()
    b.screenshot()
    assert len(shots) == 4
    b.shoot_actions = True
    b._run(lambda ws: None, shoot=False)  # and so does an explicit False
    assert len(shots) == 4


# ------------------------------------------------------ write_json_atomic ---
def test_write_json_atomic(tmp_path):
    p = tmp_path / "s.json"
    br.write_json_atomic(str(p), {"a": 1})
    assert json.loads(p.read_text()) == {"a": 1}
    errors = []

    def writer(i):
        try:
            for _ in range(20):
                br.write_json_atomic(str(p), {"i": i})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    assert json.loads(p.read_text())["i"] in range(4)
    assert os.listdir(tmp_path) == ["s.json"]  # no temp files left


# ------------------------------------------------------------ page_origin ---
def test_page_origin_env_wins(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:4111/")
    assert br.page_origin() == "http://127.0.0.1:4111"


def _default_origin():
    from fused_render._branch import branch_port

    return f"http://127.0.0.1:{branch_port()}"


def test_page_origin_server_json_then_default(monkeypatch):
    # fused-render: `<home>/server.json` (server/app.py write_server_json), and
    # the bare `fused-render` port as the default (FusedBot read its pid file
    # and fell back to 2777).
    from fused_render.bots import paths as bpaths

    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    pid = bpaths.server_json_path()
    os.makedirs(os.path.dirname(pid), exist_ok=True)
    if os.path.exists(pid):
        os.remove(pid)
    assert br.page_origin() == _default_origin()
    with open(pid, "w") as f:
        json.dump({"origin": "http://127.0.0.1:5222/"}, f)
    assert br.page_origin() == "http://127.0.0.1:5222"
    with open(pid, "w") as f:
        f.write("not json")
    assert br.page_origin() == _default_origin()
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:4111")
    assert br.page_origin() == "http://127.0.0.1:4111"


def test_page_origin_server_json_follows_fused_render_home(monkeypatch, tmp_path):
    # Inverted from FusedBot, whose own home was not fused-render's: here the
    # bots ARE fused-render, so FUSED_RENDER_HOME's server.json is the one.
    other = tmp_path / "other-home"
    other.mkdir()
    (other / "server.json").write_text(json.dumps({"origin": "http://127.0.0.1:1888"}))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(other))
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)
    assert br.page_origin() == "http://127.0.0.1:1888"


# -------------------------------------------------------------- list_files ---
def test_list_files_absolute_newest_first(tmp_path):
    d = tmp_path / "dl"
    d.mkdir()
    (d / "a.txt").write_text("a")
    (d / "b.txt").write_text("bb")
    (d / "c.crdownload").write_text("partial")
    (d / ".hidden").write_text("x")
    os.utime(d / "a.txt", (1, 1))
    b = br.Browser(str(tmp_path / "data"), str(tmp_path / "cache"))
    out = b.list_files(str(d))
    assert [f["name"] for f in out] == ["b.txt", "a.txt"]
    assert out[0]["path"] == str(d / "b.txt") and os.path.isabs(out[0]["path"])
    assert out[0]["size"] == 2
    assert b.list_files(str(tmp_path / "nope")) == []


# ------------------------------------------------------------- integration ---
PAGE = """<!doctype html><html><head><title>bot test</title></head><body>
<h1>Bot test page</h1>
<button id="plain" onclick="document.getElementById('out').textContent='plain clicked'">Plain button</button>
<label>Colour <select id="colour"><option>Red</option><option>Green</option><option>Blue</option></select></label>
<div role="dialog" aria-label="Cookie notice" style="border:1px solid #888;padding:8px">
  We use cookies <button onclick="document.getElementById('out').textContent='cookies accepted'">Accept cookies</button>
</div>
<div id="host" style="padding:4px"></div>
<p id="out">nothing yet</p>
<iframe style="width:300px;height:80px" srcdoc="<button onclick=&quot;this.textContent='Frame clicked'&quot;>Frame button</button>"></iframe>
<script>
  const root = document.getElementById('host').attachShadow({mode: 'closed'});
  const b = document.createElement('button');
  b.textContent = 'Shadow button';
  b.addEventListener('click', e => {
    document.getElementById('out').textContent = 'shadow clicked ' + (e.isTrusted ? 'trusted' : 'synthetic'); });
  const inp = document.createElement('input');
  inp.setAttribute('aria-label', 'Shadow field');
  inp.addEventListener('input', () => { document.getElementById('out').textContent = 'typed ' + inp.value; });
  root.append(b, inp);
</script>
</body></html>"""


def _at(obs, pred):
    """tools.target's kwargs for the first element matching `pred`."""
    el = next((e for e in obs["elements"] if pred(e)), None)
    assert el is not None, [(e.get("ref"), e.get("tag"), e.get("role"), e.get("text")) for e in obs["elements"]]
    kw = {"ref": el["ref"], "text": el.get("text") or "", "x": el.get("x"), "y": el.get("y")}
    if el.get("backend") is not None:
        kw["backend"] = el["backend"]
    return el, kw


@pytest.mark.skipif(not os.environ.get("BOTS_CHROME_TESTS"), reason="set BOTS_CHROME_TESTS=1 to drive a real Chrome")
def test_real_chrome_observe_and_shadow_click(tmp_path):
    b = br.Browser(str(tmp_path / "data"), str(tmp_path / "cache"))
    try:
        info = b.goto("data:text/html;charset=utf-8," + urllib.parse.quote(PAGE))
        assert info["url"].startswith("data:text/html")
        obs = b.observe()
        els = obs["elements"]
        refs = [e["ref"] for e in els]
        assert all(r.startswith("sb") for r in refs) and len(refs) == len(set(refs))

        plain, _ = _at(obs, lambda e: e.get("text") == "Plain button")
        assert plain["tag"] == "button"
        sel, sel_at = _at(obs, lambda e: e.get("tag") == "select")
        assert sel["options"] == ["Red", "Green", "Blue"] and sel["value"] == "Red"
        assert sel["text"] == "Colour" and isinstance(sel.get("backend"), int)
        dlg, _ = _at(obs, lambda e: e.get("role") == "dialog")
        assert dlg["text"] == "Cookie notice" and dlg["dialog"] is True
        acc, _ = _at(obs, lambda e: e.get("text") == "Accept cookies")
        assert acc["dialog"] is True
        shadow, shadow_at = _at(obs, lambda e: e.get("text") == "Shadow button")
        assert shadow["tag"] == "button" and isinstance(shadow.get("backend"), int)
        field, field_at = _at(obs, lambda e: e.get("text") == "Shadow field")
        assert field.get("empty") is True
        frame_btn, frame_at = _at(obs, lambda e: e.get("text") == "Frame button")

        # The closed shadow root hides the stamp from document.querySelector,
        # so only the backend node can reach these.
        hidden = b._run(lambda ws: b._eval(
            ws, f"document.querySelector('[data-sb-ref={shadow['ref']}]') === null"))[0]
        assert hidden is True

        b.click(**shadow_at)
        assert "shadow clicked trusted" in b.read()["text"]  # a real mouse event, not el.click()

        b.type("hello", ref=field_at["ref"], x=field_at["x"], y=field_at["y"], backend=field_at["backend"])
        assert "typed hello" in b.read()["text"]

        assert b.select("green", **sel_at)["chosen"] == "Green"

        b.click(**frame_at)
        obs2 = b.observe()
        assert any(e.get("text") == "Frame clicked" for e in obs2["elements"]), obs2["elements"]

        jpg = b.screenshot_jpeg(max_width=640, quality=50)
        assert jpg[:2] == b"\xff\xd8" and len(jpg) > 1000
        assert b.screenshot() == b.shot_path and os.path.isfile(b.shot_path)
        assert b.thumb_bytes and b.thumb_bytes[:2] == b"\xff\xd8"
    finally:
        b.stop()
    assert not b.alive()
