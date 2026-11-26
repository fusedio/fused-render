"""A picture of a canvas's LAYOUT, taken with no canvas page open (SPEC §28
CV-8, D1512).

The `canvas.toml` view (templates/canvas/) draws a canvas the way the
workbench lays it out, from the file alone: nothing is run, nothing is
signed in. So an agent that has just moved nodes or re-packed a folder can
look at the result without the user's canvas page, without waiting for the
edit to be published, and without the workbench re-running every node: this
module loads that view in a headless Chrome and returns one PNG.

    shot(toml_path, origin, template, focus="") -> {"png": bytes, ...} | {"error": sentence}

One short-lived Chrome per picture, with a profile of its own in a temp
folder that is removed afterwards. It is driven over the DevTools protocol
(the Bots' stdlib client, `bots.browser.WS`) for one reason: the picture is
taken when the PAGE says it is drawn (`data-cv="ready"` on its root, set
after the reader answered, the canvas was built and the typeface loaded).
Chrome's own one-shot `--screenshot` has no such signal and photographed the
loading placeholder about one time in two. The browser is the one the Bots
already look for (`bots.browser.found_chrome`); with none installed the
answer is a sentence the caller can act on.
"""
from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
from urllib.parse import urlencode

log = logging.getLogger(__name__)

#: The picture's size in CSS px, at device scale 1: under the long edge a
#: model's vision input is resized to, so no pixel of it is thrown away.
SIZE = (1568, 980)
#: How long one picture may take, browser launch to PNG. A cold Chrome on a
#: busy machine takes a few seconds; the draw itself is well under one.
TIMEOUT_S = 30.0
#: A canvas.toml larger than this is not a canvas the view offers (the
#: condition gate's own guard, templates/canvas/condition.py).
MAX_TOML_BYTES = 2 * 1024 * 1024
#: Never more than this many Chromes at once: a model that asks for five
#: pictures in one turn gets them one after another.
_SLOTS = threading.BoundedSemaphore(2)
#: The page's own signal (templates/canvas/template.html): the attribute on
#: its root element, and the two values it takes.
READY_ATTR = "data-cv"
_POLL_S = 0.1

NO_BROWSER = ("no Chrome, Chromium, Edge or Brave browser is installed, so the "
              "layout cannot be drawn here")


def names_in(doc: dict) -> list:
    """Every name a picture can be framed on: each row's `udfName`, and a
    folder's `folderName` where it differs. File order, no duplicates."""
    canvas = doc.get("canvas") if isinstance(doc, dict) else None
    rows = canvas.get("nodes") if isinstance(canvas, dict) else None
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        for key in ("udfName", "folderName"):
            name = row.get(key)
            if isinstance(name, str) and name and name not in out:
                out.append(name)
    return out


def read_canvas(toml_path: str) -> dict:
    """`{"doc": <parsed toml>}` for a readable canvas file, else `{"error":
    sentence}`. Checked before a browser is started: a file that does not
    parse would only come back as a picture of the traceback."""
    try:
        if os.path.getsize(toml_path) > MAX_TOML_BYTES:
            return {"error": "canvas.toml is too large to draw"}
        with open(toml_path, "rb") as fh:
            doc = tomllib.load(fh)
    except FileNotFoundError:
        return {"error": "this folder has no canvas.toml"}
    except OSError as exc:
        return {"error": "canvas.toml could not be read (%s)" % (exc.strerror or exc)}
    except tomllib.TOMLDecodeError as exc:
        return {"error": "canvas.toml does not parse: %s" % exc}
    if not isinstance(doc, dict) or doc.get("type") != "canvas":
        return {"error": 'canvas.toml is not a canvas (no top-level type = "canvas")'}
    return {"doc": doc}


def view_url(origin: str, template: str, toml_path: str, focus: str = "") -> str:
    """The canvas view's own page for `toml_path`, in picture mode (`shot=1`:
    the whole canvas fitted, no controls), framed on `focus` when named."""
    query = [("path", template), ("_file", toml_path), ("shot", "1")]
    if focus:
        query.append(("focus", focus))
    return origin.rstrip("/") + "/render?" + urlencode(query)


def chrome_argv(chrome: str, profile: str, size=SIZE) -> list:
    """A headless Chrome on a profile of its own, waiting on a DevTools port
    it picks itself (written to `<profile>/DevToolsActivePort`)."""
    return [
        chrome, "--headless=new", "--disable-gpu", "--mute-audio",
        "--no-first-run", "--no-default-browser-check",
        "--disable-background-networking", "--disable-sync",
        "--disable-extensions", "--hide-scrollbars",
        "--force-device-scale-factor=1",
        "--remote-debugging-port=0",
        "--user-data-dir=" + profile,
        "--window-size=%d,%d" % (size[0], size[1]),
        "about:blank",
    ]


def png_size(png: bytes) -> tuple:
    """`(width, height)` from a PNG's header (the page area is a little
    shorter than the window Chrome was asked for)."""
    if len(png) >= 24 and png[12:16] == b"IHDR":
        return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
    return SIZE


class _Failed(Exception):
    """A picture that could not be taken; the message is the sentence."""


def _find_chrome():
    from fused_render.bots import browser

    if browser._LAUNCHES_REFUSED.is_set():  # the app is quitting
        return None
    return browser.found_chrome()


def _debug_port(profile: str, proc, deadline: float) -> int:
    """The DevTools port Chrome wrote into its profile, once it is up."""
    active = os.path.join(profile, "DevToolsActivePort")
    while time.monotonic() < deadline:
        try:
            with open(active, encoding="utf-8") as fh:
                port = int(fh.readline().strip() or 0)
            if port:
                return port
        except (OSError, ValueError):
            pass
        if proc.poll() is not None:
            raise _Failed("the browser exited at launch (code %s)" % proc.returncode)
        time.sleep(_POLL_S)
    raise _Failed("the browser did not come up")


def _page_socket(port: int, deadline: float) -> str:
    """The DevTools socket of the one page Chrome opened at launch."""
    from fused_render.bots import browser

    while time.monotonic() < deadline:
        try:
            for target in browser._http(port, "/json/list") or []:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return target["webSocketDebuggerUrl"]
        except (OSError, ValueError):
            pass
        time.sleep(_POLL_S)
    raise _Failed("the browser opened no page")


def _capture(port: int, url: str, deadline: float) -> bytes:
    """Load `url` in the launch page, wait for the page's own word that the
    canvas is drawn, and take the picture."""
    from fused_render.bots import browser

    ws = browser.WS(_page_socket(port, deadline), timeout=max(1.0, deadline - time.monotonic()))
    try:
        ws.call("Page.enable")
        ws.call("Page.navigate", url=url)
        state = None
        while time.monotonic() < deadline:
            state = ws.call(
                "Runtime.evaluate", returnByValue=True,
                expression="document.documentElement.getAttribute(%r)" % READY_ATTR,
            ).get("result", {}).get("value")
            if state in ("ready", "failed"):
                break
            time.sleep(_POLL_S)
        if state == "failed":
            raise _Failed("the canvas view could not read this canvas")
        if state != "ready":
            raise _Failed("drawing the layout took too long")
        # Two frames, so what was just built is what is on the glass.
        ws.call("Runtime.evaluate", awaitPromise=True, expression=(
            "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(1))))"))
        data = ws.call("Page.captureScreenshot", format="png").get("data")
    except _Failed:
        raise
    except Exception as exc:  # noqa: BLE001 — a dropped socket, a refused call
        raise _Failed("the browser stopped answering (%s)" % exc) from None
    finally:
        ws.close()
    try:
        png = base64.b64decode(data or "")
    except ValueError:
        png = b""
    if not png.startswith(b"\x89PNG"):
        raise _Failed("the browser did not hand back a picture")
    return png


def _render(chrome: str, url: str, timeout: float) -> bytes:
    """One Chrome, one picture of `url`, nothing left behind. Raises
    `_Failed` with the sentence."""
    work = tempfile.mkdtemp(prefix="fused-render-layout-")
    profile = os.path.join(work, "profile")
    deadline = time.monotonic() + timeout
    proc = None
    try:
        try:
            # `close_fds=False` and no new session: the two things that let
            # CPython use posix_spawn here. A fork() of the server dies before
            # exec (rc -11) once libproj is resident; see
            # claude_health.SUBPROCESS_KWARGS.
            proc = subprocess.Popen(
                chrome_argv(chrome, profile), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=False)
        except OSError as exc:
            raise _Failed("the browser could not be started (%s)" % (exc.strerror or exc)) from None
        return _capture(_debug_port(profile, proc, deadline), url, deadline)
    finally:
        if proc is not None:
            _kill(proc)
        shutil.rmtree(work, ignore_errors=True)


def shot(toml_path: str, origin: str, template: str, focus: str = "",
         timeout: float = TIMEOUT_S) -> dict:
    """One PNG of the canvas `toml_path` describes.

    `{"png": bytes, "width", "height", "nodes", "folders", "edges",
    "focus"}`, or `{"error": sentence}`; a `focus` naming nothing on the
    canvas is an error that lists what could be named."""
    read = read_canvas(toml_path)
    if "error" in read:
        return read
    doc = read["doc"]
    names = names_in(doc)
    if focus and focus not in names:
        return {"error": 'nothing on this canvas is named "%s" — names: %s'
                         % (focus, ", ".join(names[:60]) or "(none)")}
    chrome = _find_chrome()
    if not chrome:
        return {"error": NO_BROWSER, "no_browser": True}
    canvas = doc.get("canvas") if isinstance(doc.get("canvas"), dict) else {}
    rows = [r for r in (canvas.get("nodes") or []) if isinstance(r, dict)]
    folders = sum(1 for r in rows if r.get("type") == "udf-folder")
    edges = canvas.get("edges")
    try:
        with _SLOTS:
            png = _render(chrome, view_url(origin, template, toml_path, focus), timeout)
    except _Failed as exc:
        return {"error": str(exc)}
    width, height = png_size(png)
    return {"png": png, "width": width, "height": height,
            "nodes": len(rows) - folders, "folders": folders,
            "edges": len(edges) if isinstance(edges, list) else 0,
            "focus": focus}


def _kill(proc) -> None:
    """End the Chrome. Asked first (SIGTERM), so it takes its helper
    processes down with it; killed if it has not gone in two seconds."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        return
    try:
        proc.kill()
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass
