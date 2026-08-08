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
