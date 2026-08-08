"""End-to-end tests for the AutoCAD (DXF) viewer template, driven through
Playwright against a real fused-render server (FUSED_RENDER_CORE_TEMPLATES points
at this checkout's templates so edits are served live).

Verifies the core+measurement feature set actually works in a browser:
  * a .dxf renders to a WebGL canvas that is not blank,
  * the drawing panel is populated from reader.py (format / units / entities),
  * every layer is listed and the visibility toggle works,
  * the distance measure tool produces a result and a persistent overlay,
  * a .dwg shows the "not yet supported" state (the DWG-later slot).

Skipped when playwright or the built React shell is missing. Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/autocad_viewer/tests/test_viewer_e2e.py -o addopts=""
"""
import io
import os
import socket
import subprocess
import sys
import time
import urllib.request
from urllib.parse import quote

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
# repo root: autocad_viewer/tests -> autocad_viewer -> templates -> fused_render -> root
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
SHELL = os.path.join(ROOT, "fused_render", "static", "shell-dist", "index.html")

pytestmark = pytest.mark.skipif(not os.path.exists(SHELL), reason="React shell not built")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    home = tmp_path_factory.mktemp("cad-home")
    work = tmp_path_factory.mktemp("cad-work")
    port = _free_port()
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["FUSED_RENDER_HOME"] = str(home)
    env["FUSED_RENDER_CORE_TEMPLATES"] = os.path.join(ROOT, "fused_render", "templates")
    log_path = home / "server.log"
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "fused_render.cli", "serve",
             "--port", str(port), "--no-browser", "--start-dir", str(work)],
            env=env, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=2).read()
            break
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError("server exited:\n" +
                                   log_path.read_text("utf-8", errors="replace")[-2000:])
            time.sleep(0.3)
    else:
        proc.kill()
        raise RuntimeError("server did not come up")
    yield {"port": port}
    proc.kill()
    proc.wait(timeout=15)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _embed_url(port, filename):
    fpath = os.path.join(FIX, filename).replace("\\", "/")
    return f"http://127.0.0.1:{port}/explorer/embed/" + quote(fpath, safe="/:")


def _wait(fn, timeout=30, step=0.25):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(step)
    raise AssertionError(f"condition not met within {timeout}s (last={last!r})")


def _open(browser, port, filename):
    """Open a fixture in the viewer; return (page, frame) once the template frame exists."""
    page = browser.new_page(viewport={"width": 1280, "height": 860})
    page.goto(_embed_url(port, filename), wait_until="domcontentloaded")

    def find_frame():
        page.keyboard.press("Escape")  # dismiss any shell welcome tour
        return next((f for f in page.frames if "/render" in f.url), None)

    frame = _wait(find_frame, timeout=30, step=0.5)
    return page, frame


def _open_ready(browser, port, filename="floorplan.dxf"):
    """Open a fixture and wait until the drawing has finished loading."""
    page, frame = _open(browser, port, filename)
    _wait(lambda: frame.evaluate(
        "() => { const c=document.querySelector('#cadHost canvas');"
        "const e=(document.getElementById('echo')||{}).textContent||'';"
        "return c && /Ready ·/.test(e); }"), timeout=40)
    return page, frame


def _zpct(frame):
    z = frame.evaluate("() => document.getElementById('zpct').textContent") or ""
    return int(z[:-1]) if z.endswith("%") and z[:-1].lstrip("-").isdigit() else None


def test_dxf_renders_and_panels_populate(server, browser):
    page, frame = _open(browser, server["port"], "floorplan.dxf")
    # wait until the viewer reports Ready with a canvas
    state = _wait(lambda: frame.evaluate(
        """() => {
            const c = document.querySelector('#cadHost canvas');
            const echo = (document.getElementById('echo')||{}).textContent||'';
            if (!c || !/Ready/.test(echo)) return null;
            return {
              layers: document.querySelectorAll('.layer-row').length,
              format: (document.getElementById('pFormat')||{}).textContent,
              units: (document.getElementById('pUnits')||{}).textContent,
              entities: (document.getElementById('pEntities')||{}).textContent,
              extents: (document.getElementById('pExtents')||{}).textContent,
              hist: document.querySelectorAll('.hbar').length,
              cw: c.width, ch: c.height,
            };
        }""") or None, timeout=40)
    assert state["layers"] == 9
    assert "DXF" in state["format"] and "2018" in state["format"]
    assert state["units"] == "Millimeters"
    assert state["entities"] == "20"
    assert "→" in state["extents"] and state["extents"] != "computed on load"
    assert state["hist"] == 6
    assert state["cw"] > 100 and state["ch"] > 100
    page.close()


def test_canvas_actually_draws_pixels(server, browser):
    from PIL import Image
    page, frame = _open(browser, server["port"], "floorplan.dxf")
    _wait(lambda: frame.evaluate(
        "() => { const c=document.querySelector('#cadHost canvas');"
        "const e=(document.getElementById('echo')||{}).textContent||'';"
        "return c && /Ready/.test(e); }"), timeout=40)
    time.sleep(0.6)  # let the WebGL frame composite
    png = frame.locator("#cadHost canvas").screenshot()
    im = Image.open(io.BytesIO(png)).convert("RGB")
    colors = im.getcolors(maxcolors=1 << 20) or []
    # a blank clear-color canvas is a single color; a rendered drawing has many
    assert len(colors) > 5, f"canvas looks blank ({len(colors)} distinct colors)"
    page.close()


def test_layer_toggle_hides_and_show_all_restores(server, browser):
    page, frame = _open(browser, server["port"], "floorplan.dxf")
    _wait(lambda: frame.evaluate(
        "() => document.querySelectorAll('.layer-row').length === 9"), timeout=40)
    frame.locator(".layer-row .eye").first.click()
    off = _wait(lambda: "off" in (frame.locator(".layer-row").first.get_attribute("class") or ""))
    assert off
    frame.locator("#allOn").click()
    restored = _wait(lambda: "off" not in (frame.locator(".layer-row").first.get_attribute("class") or ""))
    assert restored
    page.close()


def test_measure_distance(server, browser):
    page, frame = _open(browser, server["port"], "floorplan.dxf")
    _wait(lambda: frame.evaluate(
        "() => { const c=document.querySelector('#cadHost canvas');"
        "const e=(document.getElementById('echo')||{}).textContent||'';"
        "return c && /Ready/.test(e); }"), timeout=40)
    res = frame.evaluate(
        """() => {
            const canvas = document.querySelector('#cadHost canvas');
            const r = canvas.getBoundingClientRect();
            document.querySelector('.tbtn[data-tool="distance"]').click();
            const pd = (x, y) => { for (const t of ['pointermove','pointerdown','pointerup'])
              canvas.dispatchEvent(new PointerEvent(t, {bubbles:true,cancelable:true,
                clientX:r.left+x, clientY:r.top+y, button:0, pointerId:1, isPrimary:true})); };
            pd(300, 300); pd(650, 480);
            return { out: document.getElementById('measureOut').textContent,
                     echo: document.getElementById('echo').textContent,
                     overlay: document.getElementById('overlay').querySelectorAll('*').length };
        }""")
    assert "Distance" in res["out"]
    assert "mm" in res["out"]
    assert "Distance =" in res["echo"]
    assert res["overlay"] > 0, "completed measurement should persist on the overlay"
    page.close()


def test_measure_point_lands_under_cursor_after_zoom(server, browser):
    # regression: dxf-viewer drives wheel zoom via camera.zoom, so the model<->canvas
    # transforms must honor it or the placed point drifts away from the cursor.
    page, frame = _open(browser, server["port"], "floorplan.dxf")
    _wait(lambda: frame.evaluate(
        "() => { const c=document.querySelector('#cadHost canvas');"
        "const e=(document.getElementById('echo')||{}).textContent||'';"
        "return c && /Ready ·/.test(e); }"), timeout=40)
    res = frame.evaluate(
        """() => {
            const canvas = document.querySelector('#cadHost canvas');
            const r = canvas.getBoundingClientRect();
            const os = document.getElementById('tgOsnap');
            if (os.classList.contains('on')) os.click();   // no snapping: test the raw transform
            const cx = r.left + r.width/2, cy = r.top + r.height/2;
            for (let i=0;i<4;i++) canvas.dispatchEvent(new WheelEvent('wheel',
              {deltaY:-120, clientX:cx, clientY:cy, bubbles:true, cancelable:true}));
            const zoom = document.getElementById('zpct').textContent;
            document.querySelector('.tbtn[data-tool="distance"]').click();
            const px = r.width*0.62, py = r.height*0.38;
            for (const t of ['pointermove','pointerdown'])
              canvas.dispatchEvent(new PointerEvent(t, {bubbles:true,cancelable:true,
                clientX:r.left+px, clientY:r.top+py, button:0, pointerId:1, isPrimary:true}));
            const node = document.querySelector('#overlay circle.ov-node');
            return {zoom, px, py, cx: node && +node.getAttribute('cx'), cy: node && +node.getAttribute('cy')};
        }""")
    assert res["zoom"] != "100%", f"wheel zoom did not take effect (zoom={res['zoom']!r})"
    assert res["cx"] is not None, "no measurement node was drawn"
    assert abs(res["cx"] - res["px"]) <= 2 and abs(res["cy"] - res["py"]) <= 2, \
        f"measurement point not under cursor after zoom: {res}"
    page.close()


def _isolate_layer(frame, name):
    """Click the isolate (solo) button on the layer row whose raw name is `name`."""
    return frame.evaluate(
        """(name) => {
            for (const row of document.querySelectorAll('.layer-row')) {
                const n = row.querySelector('.lname');
                if (n && n.getAttribute('title') === name) {
                    row.querySelector('.solo').click();
                    return true;
                }
            }
            return false;
        }""", name)


def test_layer_isolate_and_restore(server, browser):
    page, frame = _open(browser, server["port"], "floorplan.dxf")
    _wait(lambda: frame.evaluate(
        "() => document.querySelectorAll('.layer-row').length === 9"), timeout=40)
    assert _isolate_layer(frame, "WALLS"), "WALLS layer / solo button not found"
    # isolate => exactly one row on (WALLS), the other eight off
    state = _wait(lambda: frame.evaluate(
        """() => {
            const rows = [...document.querySelectorAll('.layer-row')];
            const on = rows.filter(r => !r.classList.contains('off'));
            return (rows.length === 9 && on.length === 1)
                ? on[0].querySelector('.lname').getAttribute('title') : null;
        }""") or None)
    assert state == "WALLS"
    frame.locator("#allOn").click()
    restored = _wait(lambda: frame.evaluate(
        "() => [...document.querySelectorAll('.layer-row')].every(r => !r.classList.contains('off'))"))
    assert restored
    page.close()


def test_text_layer_renders_glyphs(server, browser):
    from PIL import Image
    page, frame = _open(browser, server["port"], "floorplan.dxf")
    _wait(lambda: frame.evaluate(
        "() => { const c=document.querySelector('#cadHost canvas');"
        "const e=(document.getElementById('echo')||{}).textContent||'';"
        "return c && /Ready/.test(e); }"), timeout=40)
    # isolate the TEXT layer so ONLY text geometry can appear on the canvas
    assert _isolate_layer(frame, "TEXT"), "TEXT layer not found"
    _wait(lambda: frame.evaluate(
        """() => {
            const on = [...document.querySelectorAll('.layer-row')].filter(r => !r.classList.contains('off'));
            return on.length === 1 && on[0].querySelector('.lname').getAttribute('title') === 'TEXT';
        }"""))
    time.sleep(0.7)  # let WebGL recomposite the isolated view
    png = frame.locator("#cadHost canvas").screenshot()
    im = Image.open(io.BytesIO(png)).convert("RGB")
    colors = im.getcolors(maxcolors=1 << 20) or []
    # only TEXT is visible: many colors => glyphs rendered; ~1 color => no font applied
    assert len(colors) > 3, f"TEXT layer produced no glyphs ({len(colors)} colors) — font not applied"
    page.close()


def test_dwg_shows_unsupported_state(server, browser):
    page, frame = _open(browser, server["port"], "fake.dwg")
    txt = _wait(lambda: frame.evaluate(
        """() => {
            const st = document.getElementById('state');
            if (!st || !st.classList.contains('show')) return null;
            const t = (document.getElementById('stateBig')||{}).textContent || '';
            return /DWG|supported/i.test(t) ? t : null;
        }""") or None, timeout=40)
    assert "DWG" in txt and "not yet supported" in txt.lower()
    page.close()


def test_layer_counts_and_empty_flag(server, browser):
    page, frame = _open_ready(browser, server["port"])
    rows = _wait(lambda: frame.evaluate(
        """() => {
            const rs = [...document.querySelectorAll('.layer-row')];
            if (rs.length !== 9) return null;
            const m = {};
            for (const r of rs) m[r.querySelector('.lname').getAttribute('title')] =
                { count: r.querySelector('.lcount').textContent, empty: r.classList.contains('empty') };
            return m;
        }""") or None)
    assert rows["WALLS"] == {"count": "4", "empty": False}
    assert rows["TEXT"]["count"] == "3"
    assert rows["ELECTRICAL"]["count"] == "3"
    # layers with no geometry are flagged so you can see which layers hold data
    assert rows["0"] == {"count": "0", "empty": True}
    assert rows["Defpoints"]["empty"] is True
    assert sum(int(v["count"]) for v in rows.values()) == 20
    page.close()


def test_layer_eye_is_borderless(server, browser):
    # the eye <button> must reset the UA button box or the rows look misaligned
    page, frame = _open_ready(browser, server["port"])
    style = _wait(lambda: frame.evaluate(
        """() => {
            const eye = document.querySelector('.layer-row .eye');
            if (!eye) return null;
            const cs = getComputedStyle(eye);
            return { bg: cs.backgroundColor, border: cs.borderStyle };
        }""") or None)
    assert style["border"] == "none"
    assert style["bg"] in ("rgba(0, 0, 0, 0)", "transparent")
    page.close()


def test_grid_toggle_draws_and_clears(server, browser):
    page, frame = _open_ready(browser, server["port"])
    res = frame.evaluate(
        """() => {
            const tg = document.getElementById('tgGrid');
            tg.click();
            const on = document.querySelectorAll('#overlay .ov-grid').length;
            const major = document.querySelectorAll('#overlay .ov-grid.major').length;
            const cls = tg.classList.contains('on');
            tg.click();
            const off = document.querySelectorAll('#overlay .ov-grid').length;
            return { on, major, off, cls };
        }""")
    assert res["cls"] is True
    assert res["on"] > 4, f"grid should draw reference lines (got {res['on']})"
    assert res["major"] >= 1, "every 5th line should be styled as a major line"
    assert res["off"] == 0
    page.close()


def test_shortcuts_ignored_while_typing_in_layer_filter(server, browser):
    # regression: f / m / Enter must not fire while the layer filter has focus
    page, frame = _open_ready(browser, server["port"])
    res = frame.evaluate(
        """() => {
            const inp = document.getElementById('layerFilter');
            inp.focus();
            for (const k of ['m', 'f'])
              inp.dispatchEvent(new KeyboardEvent('keydown', {key:k, bubbles:true, cancelable:true}));
            return { toolActive: !!document.querySelector('.tbtn[data-tool].active'),
                     echo: document.getElementById('echo').textContent };
        }""")
    assert res["toolActive"] is False, "typing in the filter must not trigger tool shortcuts"
    assert "Specify" not in res["echo"]
    page.close()


def test_angle_measure_draws_two_rays_from_the_vertex(server, browser):
    # regression: angle is a vertex + two rays, not a V->A->B polyline
    page, frame = _open_ready(browser, server["port"])
    res = frame.evaluate(
        """() => {
            const canvas = document.querySelector('#cadHost canvas');
            const r = canvas.getBoundingClientRect();
            const os = document.getElementById('tgOsnap'); if (os.classList.contains('on')) os.click();
            document.querySelector('.tbtn[data-tool="angle"]').click();
            const click = (x, y) => { for (const t of ['pointermove','pointerdown','pointerup'])
              canvas.dispatchEvent(new PointerEvent(t, {bubbles:true,cancelable:true,
                clientX:r.left+x, clientY:r.top+y, button:0, pointerId:1, isPrimary:true})); };
            click(r.width*0.40, r.height*0.60);   // vertex
            click(r.width*0.70, r.height*0.60);   // first ray
            click(r.width*0.40, r.height*0.30);   // second ray
            const rays = [...document.querySelectorAll('#overlay line.ov-line')].map(l => ({
              x1:+l.getAttribute('x1'), y1:+l.getAttribute('y1'),
              x2:+l.getAttribute('x2'), y2:+l.getAttribute('y2')}));
            return { out: document.getElementById('measureOut').textContent, rays };
        }""")
    assert "Angle" in res["out"]
    rays = res["rays"]
    assert len(rays) == 2, f"angle should draw two rays, got {len(rays)}"
    # both rays start at the shared vertex (their first endpoint)
    assert abs(rays[0]["x1"] - rays[1]["x1"]) < 1 and abs(rays[0]["y1"] - rays[1]["y1"]) < 1, \
        f"rays do not share a vertex: {rays}"
    page.close()


def test_measure_label_has_backing_chip(server, browser):
    # the measurement value must sit on a chip so it stays legible over the drawing
    page, frame = _open_ready(browser, server["port"])
    res = frame.evaluate(
        """() => {
            const canvas = document.querySelector('#cadHost canvas');
            const r = canvas.getBoundingClientRect();
            document.querySelector('.tbtn[data-tool="distance"]').click();
            const pd = (x, y) => { for (const t of ['pointermove','pointerdown','pointerup'])
              canvas.dispatchEvent(new PointerEvent(t, {bubbles:true,cancelable:true,
                clientX:r.left+x, clientY:r.top+y, button:0, pointerId:1, isPrimary:true})); };
            pd(r.width*0.3, r.height*0.5); pd(r.width*0.7, r.height*0.5);
            return { bg: document.querySelectorAll('#overlay .ov-lbl-bg').length,
                     txt: document.querySelectorAll('#overlay .ov-txt').length };
        }""")
    assert res["bg"] >= 1, "measurement label needs a background chip"
    assert res["txt"] >= 1
    page.close()


def test_fullscreen_button_requests_fullscreen(server, browser):
    page, frame = _open_ready(browser, server["port"])
    called = frame.evaluate(
        """() => {
            let called = false;
            document.documentElement.requestFullscreen = () => { called = true; return Promise.resolve(); };
            document.getElementById('fsBtn').click();
            return called;
        }""")
    assert called, "full-screen button should request fullscreen"
    page.close()


def test_view_buttons_change_zoom(server, browser):
    page, frame = _open_ready(browser, server["port"])
    _wait(lambda: _zpct(frame) == 100)          # zoom-extents on load == 100%
    frame.locator("#zin").click()
    assert _wait(lambda: (_zpct(frame) or 0) > 105), "zoom-in should raise the zoom %"
    frame.locator("#zout").click()
    frame.locator("#fitBtn").click()
    assert _wait(lambda: _zpct(frame) is not None and abs(_zpct(frame) - 100) <= 2), \
        "zoom-extents should return to ~100%"
    page.close()


def test_panel_and_theme_toggles(server, browser):
    page, frame = _open_ready(browser, server["port"])
    cls = lambda: frame.locator("#app").get_attribute("class") or ""
    frame.locator("#leftToggle").click()
    assert _wait(lambda: "no-left" in cls())
    frame.locator("#leftToggle").click()
    assert _wait(lambda: "no-left" not in cls())
    frame.locator("#rightToggle").click()
    assert _wait(lambda: "no-right" in cls())
    theme = lambda: frame.evaluate("() => document.documentElement.getAttribute('data-theme')")
    before = theme()
    frame.locator("#themeBtn").click()
    assert _wait(lambda: theme() != before), "theme button should flip the chrome theme"
    page.close()


def test_status_toggles_flip(server, browser):
    page, frame = _open_ready(browser, server["port"])
    res = frame.evaluate(
        """() => {
            const osnap = document.getElementById('tgOsnap');
            const ortho = document.getElementById('tgOrtho');
            const before = { osnap: osnap.classList.contains('on'), ortho: ortho.classList.contains('on') };
            osnap.click(); ortho.click();
            return { before, osnap: osnap.classList.contains('on'), ortho: ortho.classList.contains('on') };
        }""")
    assert res["before"]["osnap"] is True and res["osnap"] is False   # OSNAP starts on, toggles off
    assert res["before"]["ortho"] is False and res["ortho"] is True   # ORTHO starts off, toggles on
    page.close()
