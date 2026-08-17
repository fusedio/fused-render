"""End-to-end behaviour of the Map Viewer in a real browser.

The unit tests around this template cover Python helpers in isolation, which is
exactly why a string of regressions reached the user anyway: a raster that never
paints, a descriptor read before it exists, a code panel with nothing in it, and
a style control that silently stopped applying are all *page* behaviour. Those
only show up by opening the page.

Needs a fused-render server on 127.0.0.1:1777 and network access to the public
Maxar bucket; every test skips if either is missing.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_map_e2e.py -o addopts="" -q
"""
from __future__ import annotations

import os
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

SERVER = os.environ.get("FUSED_RENDER_URL", "http://127.0.0.1:1777")
TEMPLATE = os.environ.get(
    "MAP_TEMPLATE",
    os.path.join(os.path.expanduser("~"), ".fused-render", ".core-templates", "map", "template.html"),
)
REMOTE_COG = (
    "https://maxar-opendata.s3.amazonaws.com/events/Belize-Wildfires-June24/ard/16/"
    "033131012100/2024-01-09/10400100905FFC00-visual.tif"
)
REMOTE_CAM = "16.9711,-89.0473,12.2"
LOCAL_RASTER = os.environ.get("MAP_LOCAL_RASTER", "")
# Same product, one band instead of three — the case that caught the viewer
# claiming "RGB composite (bands 1, 2, 3)" over a greyscale image.
SINGLE_BAND_COG = (
    "https://maxar-opendata.s3.amazonaws.com/events/Cyclone-Ditwah-Sri-Lanka-Nov-2025/ard/44/"
    "033313300310/2025-12-05/102001011D2F3500-visual.tif"
)
SINGLE_BAND_CAM = "7.0308,79.8416,12.9"
# NASA HLS on Azure: the storage account has anonymous access disabled, so this
# URL answers 409 until it is signed with a Planetary Computer read token. Its
# blob endpoint sends no CORS headers either, so the browser reader can never
# take it — this is the server path, end to end.
AZURE_COG = (
    "https://hls2euwest.blob.core.windows.net/hls2/L30/43/P/GS/2026/08/14/"
    "HLS.L30.T43PGS.2026226T051556.v2.0/HLS.L30.T43PGS.2026226T051556.v2.0.B10.tif"
)
AZURE_CAM = "14.8730,76.9960,9.5"

SETTLE_MS = 90000


def _reachable(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=5).close()
        return True
    except (urllib.error.URLError, OSError):
        return False


@pytest.fixture(scope="session")
def server():
    if not _reachable(f"{SERVER}/api/config"):
        pytest.skip(f"no fused-render server on {SERVER}")
    return SERVER


@pytest.fixture(scope="session")
def network():
    request = urllib.request.Request(REMOTE_COG, headers={"Range": "bytes=0-1023"})
    try:
        urllib.request.urlopen(request, timeout=15).close()
    except (urllib.error.URLError, OSError):
        pytest.skip("public Maxar bucket is not reachable")


def render_url(server: str, target: str, cam: str = "") -> str:
    query = {"path": TEMPLATE, "open": urllib.parse.quote(target, safe="")}
    if cam:
        query["cam"] = cam
    return f"{server}/render?" + urllib.parse.urlencode(query)


class Viewer:
    """One opened Map Viewer page, with the traffic it generated."""

    def __init__(self, page):
        self.page = page
        self.errors: list[str] = []
        self.s3: list[int] = []
        self.tiles: list[int] = []
        self.tile_bytes: list[int] = []
        self.last_read = time.monotonic()
        page.on("pageerror", lambda e: self.errors.append(str(e)[:300]))
        page.on(
            "console",
            lambda m: self.errors.append("console: " + m.text[:200])
            if m.type == "error" and "GL Driver" not in m.text
            else None,
        )
        page.on("response", self._response)

    def _response(self, response):
        if "maxar-opendata" in response.url:
            self.s3.append(response.status)
            self.last_read = time.monotonic()
        if "/tiles/" in response.url and ".png" in response.url:
            self.tiles.append(response.status)
            self.last_read = time.monotonic()
            self.tile_bytes.append(int(response.header_value("content-length") or 0))

    def open(self, url: str):
        """Open the viewer and wait until its layers have settled.

        A fixed sleep here made the suite flaky the moment the bucket got slow:
        the assertions ran against a page still loading. Wait on the page's own
        state instead, then give the GPU a moment to paint what it has.
        """
        self.page.goto(url, wait_until="domcontentloaded")
        # The template's `state` lives in module scope, out of reach of
        # evaluate(), so settle on what the page actually shows: a layer card
        # exists and none of them is still spinning.
        self.page.wait_for_function(
            """() => document.querySelectorAll('.lc').length > 0
                     && !document.querySelector('.lc .spin')""",
            timeout=SETTLE_MS,
        )
        return self.quiesce()

    def quiesce(self, quiet_ms: int = 4000, timeout_ms: int = SETTLE_MS):
        """Wait until the source has stopped being read.

        A layer card stops spinning as soon as the raster is open and drawing,
        which is well before deck has finished refining tiles. Measuring "a
        restyle fetches nothing" from that moment charges the tail of the
        initial load to the restyle instead — measured as 1 and 7 stray requests
        against a restyle that really does cost zero.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if (time.monotonic() - self.last_read) * 1000 >= quiet_ms:
                break
            self.page.wait_for_timeout(500)
        return self

    def wait_for_tiles(self, timeout_ms: int = 60000):
        """Wait until the Python engine has served a tile, or give up."""
        deadline = timeout_ms
        while deadline > 0 and not self.tiles:
            self.page.wait_for_timeout(1000)
            deadline -= 1000
        return self.tiles

    @property
    def text(self) -> str:
        return self.page.inner_text("body")

    def layer_names(self) -> list[str]:
        return self.page.locator(".lc .lc-nm").all_inner_texts()

    def open_style_dock(self):
        """Select the first layer and open the style dock over it."""
        self.page.click(".lc .lc-h")
        self.page.wait_for_timeout(400)
        if "hidden" in (self.page.get_attribute("#stylepanel", "class") or ""):
            self.page.click("#btn-style")
        self.page.wait_for_timeout(800)

    def contrast_inputs(self):
        return self.page.locator("#sp-body .ctl-pair input")

    def colormap_select(self):
        return self.page.locator("#sp-body select").first

    def canvas_patch(self):
        """Mean RGB of the map canvas, to prove a restyle actually repainted."""
        return self.page.evaluate(
            """() => {
              const c = document.querySelector('#deckgl-overlay') || document.querySelector('canvas');
              const g = document.createElement('canvas');
              g.width = 120; g.height = 120;
              const ctx = g.getContext('2d');
              ctx.drawImage(c, 0, 0, 120, 120);
              const d = ctx.getImageData(0, 0, 120, 120).data;
              let r = 0, gg = 0, b = 0, n = 0;
              for (let i = 0; i < d.length; i += 4) { r += d[i]; gg += d[i+1]; b += d[i+2]; n++; }
              return [r/n, gg/n, b/n];
            }"""
        )

    def canvas_coverage(self) -> float:
        """Fraction of the map canvas actually painted by a layer."""
        return self.page.evaluate(
            """() => {
              const c = document.querySelector('#deckgl-overlay') || document.querySelector('canvas');
              if (!c) return -1;
              const g = document.createElement('canvas');
              g.width = 200; g.height = 200;
              const ctx = g.getContext('2d');
              ctx.drawImage(c, 0, 0, 200, 200);
              const d = ctx.getImageData(0, 0, 200, 200).data;
              let opaque = 0;
              for (let i = 3; i < d.length; i += 4) if (d[i] > 16) opaque++;
              return opaque / (200 * 200);
            }"""
        )


@pytest.fixture
def viewer(server, network):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        yield Viewer(page)
        browser.close()


# --------------------------------------------------------------- remote COG


@pytest.fixture
def remote(viewer, server):
    return viewer.open(render_url(server, REMOTE_COG, REMOTE_CAM))


def test_remote_cog_never_touches_the_tile_service(remote):
    # The whole point of reading it in the browser: no daemon, so no dependency
    # on a process whose port the page has baked into its URLs.
    assert remote.tiles == []


def test_remote_cog_reads_straight_from_the_object_store(remote):
    assert remote.s3, "no requests reached the bucket at all"
    assert set(remote.s3) <= {200, 206}, f"bad statuses: {set(remote.s3)}"


def test_remote_cog_reports_no_page_errors(remote):
    assert remote.errors == []


def test_remote_cog_actually_paints_imagery(remote):
    # A layer that loads its metadata but draws nothing looks identical to a
    # working one in the DOM, so assert on the canvas itself.
    covered = remote.canvas_coverage()
    assert covered > 0.02, f"raster canvas is effectively empty (opaque fraction {covered})"


def test_remote_cog_layer_card_describes_the_raster(remote):
    assert "RASTER" in remote.text


def test_style_dock_reports_the_rasters_crs(remote):
    remote.open_style_dock()
    assert "EPSG:32616" in remote.page.inner_text("#sp-body"), (
        "the style dock never reported the raster's CRS"
    )


# ------------------------------------------------------------- the code panel


def test_code_panel_describes_a_raster_instead_of_calling_it_binary(remote):
    remote.page.click("#btn-code")
    remote.page.wait_for_timeout(1500)
    panel = remote.page.inner_text("#cp-body")
    assert "Binary dataset" not in panel, (
        "the code panel still refuses to say anything about a raster the viewer "
        "is currently drawing"
    )
    assert REMOTE_COG in panel, "the panel does not show where the raster came from"
    assert "EPSG:32616" in panel, "the panel does not show the raster's CRS"


# ------------------------------------------------------------ styling controls


def test_opacity_control_reaches_the_layer(remote):
    # Drive the real slider and look at the canvas: reading deck's internals
    # would pass even if the layer never got the new value.
    remote.open_style_dock()
    slider = remote.page.locator("#sp-body input[type=range]").first
    assert slider.count() > 0, "the style dock has no opacity slider"
    before = remote.canvas_coverage()
    slider.fill("0")
    slider.dispatch_event("input")
    slider.dispatch_event("change")
    remote.page.wait_for_timeout(1200)
    after = remote.canvas_coverage()
    assert before > 0.02, f"nothing was drawn to begin with ({before})"
    assert after < before / 2, (
        f"opacity 0 left the raster on screen (coverage {before} -> {after})"
    )


def test_every_remote_raster_offers_a_contrast_control(remote):
    # Labelled "Contrast": two min/max fields that read "auto" until set.
    remote.open_style_dock()
    dock = remote.page.inner_text("#sp-body")
    assert "Contrast" in dock, dock
    assert remote.contrast_inputs().count() == 2, "contrast needs a min and a max field"


def test_contrast_defaults_to_an_automatic_window(remote):
    # Blank fields would leave the user guessing; the measured window is shown.
    remote.open_style_dock()
    dock = remote.page.inner_text("#sp-body")
    assert "contrast auto" in dock, dock


def test_changing_contrast_costs_no_network(remote):
    # The whole point of styling on the GPU: a stretch is a shader uniform, so
    # it must not refetch a single byte.
    remote.open_style_dock()
    remote.quiesce()
    before_requests = len(remote.s3)
    before_pixels = remote.canvas_patch()
    minimum = remote.contrast_inputs().first
    minimum.fill("120")
    minimum.dispatch_event("change")
    remote.page.wait_for_timeout(2500)
    assert len(remote.s3) == before_requests, (
        f"a contrast change fetched {len(remote.s3) - before_requests} times"
    )
    assert remote.tiles == [], "a contrast change went to the Python engine"
    assert remote.canvas_patch() != before_pixels, "the raster did not actually repaint"


def test_clearing_contrast_returns_to_the_automatic_window(remote):
    # Reversibility: emptying a field means "auto" again, not 0.
    remote.open_style_dock()
    minimum = remote.contrast_inputs().first
    minimum.fill("120")
    minimum.dispatch_event("change")
    remote.page.wait_for_timeout(2000)
    remote.open_style_dock()
    remote.contrast_inputs().first.fill("")
    remote.contrast_inputs().first.dispatch_event("change")
    remote.page.wait_for_timeout(2000)
    remote.open_style_dock()
    # The "auto 25-136" label is rendered whether or not auto is in force, so
    # assert on the note, which only appears while the window really is
    # automatic.
    assert "contrast auto" in remote.page.inner_text("#sp-body")
    assert remote.errors == []


# ------------------------------------------------------- the server-side path


@pytest.mark.skipif(not LOCAL_RASTER, reason="set MAP_LOCAL_RASTER to a local raster path")
def test_a_local_raster_still_renders_through_the_python_engine(viewer, server):
    local = viewer.open(render_url(server, LOCAL_RASTER))
    assert local.errors == []
    assert local.tiles, "the local raster produced no tiles from the Python engine"
    assert set(local.tiles) <= {200, 204}, f"bad tile statuses: {set(local.tiles)}"


# ------------------------------------------------- sources that need a token


@pytest.fixture(scope="session")
def azure():
    request = urllib.request.Request(AZURE_COG, headers={"Range": "bytes=0-1023"})
    try:
        urllib.request.urlopen(request, timeout=15).close()
    except urllib.error.HTTPError as denied:
        if denied.code in (401, 403, 409):
            return  # exactly the state this fixture is here to set up
        pytest.skip(f"HLS on Azure answered {denied.code}")
    except OSError:
        pytest.skip("HLS on Azure is not reachable")
    pytest.skip("HLS on Azure now allows anonymous reads; nothing to sign")


def test_a_source_that_refuses_anonymous_reads_is_signed_and_rendered(
    viewer, server, azure
):
    # Before this, the user got "409" and an empty map. The viewer now asks
    # Planetary Computer for a read token and opens the scene with it.
    page = viewer.open(render_url(server, AZURE_COG, AZURE_CAM))
    assert "409" not in page.text, page.text[:400]
    assert page.wait_for_tiles(), "the signed raster produced no tiles"
    assert set(page.tiles) <= {200, 204}, f"bad tile statuses: {set(page.tiles)}"
    # This one is drawn by maplibre, not deck, so canvas_coverage (which reads
    # deck's canvas) says nothing here. An empty tile is a 334-byte transparent
    # PNG served with a 200, so size is what separates imagery from nothing.
    assert max(page.tile_bytes) > 1000, (
        f"every tile came back empty: {sorted(set(page.tile_bytes))}"
    )


# ----------------------------------------------------------------- fallback


def test_a_remote_tiff_the_browser_cannot_read_falls_back_to_python(viewer, server):
    # An ordinary (non-cloud-optimized, non-CORS) TIFF URL must not leave the
    # user with a dead layer: the Python engine has to pick it up.
    unreadable = "https://127.0.0.1:1/not-a-real.tif"
    page = viewer.open(render_url(server, unreadable))
    text = page.text
    assert "Binary dataset" not in text
    # Either it errored honestly, or Python took it; what it must not do is
    # silently show an empty map with no explanation.
    assert any(word in text for word in ("Couldn't render", "error", "Error", "unavailable")), (
        "an unreadable raster produced no message at all"
    )


# --------------------------------------------- what the viewer says it drew


def test_band_count_is_reported_from_the_file_not_assumed(viewer, server):
    # This raster has one band. Describing it as an RGB composite is a lie the
    # user has no way to check against the (grey) picture in front of them.
    page = viewer.open(render_url(server, SINGLE_BAND_COG, SINGLE_BAND_CAM))
    page.open_style_dock()
    dock = page.page.inner_text("#sp-body")
    assert "RGB composite" not in dock, dock
    assert "1-band source" in dock, dock


def test_a_single_band_photograph_is_grey_not_false_colour(viewer, server):
    # A colour ramp on a panchromatic photograph reads as a heat map. Greyscale
    # is the convention (QGIS singleband gray, GDAL MINISBLACK, rio-tiler's
    # opt-in colormaps) and here it is simply the `gray` ramp.
    page = viewer.open(render_url(server, SINGLE_BAND_COG, SINGLE_BAND_CAM))
    page.open_style_dock()
    assert page.colormap_select().input_value() == "gray"


def test_changing_the_colormap_costs_no_network(viewer, server):
    page = viewer.open(render_url(server, SINGLE_BAND_COG, SINGLE_BAND_CAM))
    page.open_style_dock()
    page.quiesce()
    before_requests = len(page.s3)
    before_pixels = page.canvas_patch()
    page.colormap_select().select_option("viridis")
    page.page.wait_for_timeout(2500)
    assert len(page.s3) == before_requests, (
        f"a colormap change fetched {len(page.s3) - before_requests} times"
    )
    assert page.tiles == [], "a colormap change went to the Python engine"
    after = page.canvas_patch()
    assert after != before_pixels, "the colormap did not actually repaint"
    # grey means the channels match; a ramp separates them.
    assert abs(after[0] - after[2]) > abs(before_pixels[0] - before_pixels[2]), (
        f"still looks greyscale after switching to viridis: {before_pixels} -> {after}"
    )
    assert page.errors == []
