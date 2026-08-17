"""The live preview thumbnail: the picture WHILE it is being denoised.

`fused.ai.image` writes its PNG once, at the end. A FLUX render is minutes, and
for all of them the only thing on screen is a step counter — a progress bar for
a process whose whole output is visual. This module is that fixed: one small
PNG beside the image the request already named, overwritten every step, which a
page points an `<img>` at.

The rules being pinned here, in order of how expensive they are to get wrong:

- **The projection is arithmetic, not a decode.** A 128-channel FLUX.2 token
  goes to RGB through one fitted linear map; a transposed matrix or a dropped
  bias produces a plausible-looking thumbnail that is wrong in a way no eye
  catches on a 32x32 image, so the orientation is asserted rather than
  eyeballed.
- **The frame is the DENOISED ESTIMATE, never the raw latent.** klein is
  step-wise distilled and its sigma is still >= 0.5 at step 14 of 16; projecting
  what the callback holds shows noise for almost the whole render. Two
  consecutive latents and their two sigmas recover the model's current guess at
  the finished image, and that is what gets written.
- **A reader never sees half a PNG.** The page reads this file through
  `/api/fs/raw` while the worker rewrites it, so each frame lands by
  `os.replace` from a temp file in the same directory — the byte-level analogue
  of the whole-line flush rule `partial.py` documents.
- **It is always removed.** On success, on cancel AND on error, which is where
  this diverges from `partial.Sink`: a half-denoised 32x32 thumbnail is not
  salvage the way 80 minutes of transcript is.
- **One implementation, two engines.** A second copy under either image runner
  would be free to drift and would fail no behavioural test, because both copies
  would pass their own. The structural half of that is pinned at the bottom.

No render happens here and none can: the real thing needs 10.8GB of weights and
minutes of Metal. The arithmetic below was fitted and validated against a real
GGUF render by hand (see `preview.py`'s module docstring for the provenance);
what these tests defend is that the shipped code still does what was measured.
"""
import importlib.util
import os
import struct

import pytest

numpy = pytest.importorskip("numpy")

_RUNNERS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners",
)
PREVIEW_PATH = os.path.join(_RUNNERS, "preview.py")

#: The one model key the shipped table has an entry for — the class name of
#: FLUX.2 klein's VAE, which is what BOTH runners hand the sink (the torch one
#: reads it off `type(pipe.vae).__name__`, the MLX one off its variant recipe).
KEY = "AutoencoderKLFlux2"


def _by_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def preview():
    """Imported by PATH, the way a runner reaches it — which is the reading that
    ships. The server imports the same file as a package module to derive the
    path it advertises, and `test_the_SERVER_reaches_the_same_module` below
    pins that the two readings agree."""
    return _by_path("runners_preview", PREVIEW_PATH)


def _png_size(path):
    """`(width, height)` straight out of the IHDR — no PIL, so a truncated file
    fails here rather than being quietly repaired by a decoder."""
    with open(path, "rb") as handle:
        header = handle.read(24)
    assert header[:8] == b"\x89PNG\r\n\x1a\n", header[:8]
    return struct.unpack(">II", header[16:24])


def _pixels(path):
    from PIL import Image

    with Image.open(path) as image:
        data = image.convert("RGB").tobytes()
    return [tuple(data[at:at + 3]) for at in range(0, len(data), 3)]


def _tokens(value, count=16, channels=128):
    return numpy.full((count, channels), value, dtype=numpy.float32)


class Cancelled(Exception):
    """Stands in for `worker_base.Cancelled`. Unlike `partial.Sink`, the preview
    sink takes no `cancelled=` argument at all — every exit discards, so it
    never has to tell one exception from another."""


# -- the path the route, the workers and the page must agree on ------------------


def test_the_preview_path_is_a_SIBLING_of_the_png_the_request_named(preview):
    assert preview.preview_path("/x/y/20260101-120000-abc.png") == (
        "/x/y/20260101-120000-abc.preview.png")


def test_the_preview_path_replaces_the_extension_rather_than_appending(preview):
    """`out.png.preview.png` would sort beside the render and read like one, in
    a directory (`ai/images/`) a user browses."""
    assert preview.preview_path("/x/out.png").endswith("/out.preview.png")


def test_an_empty_out_path_has_no_preview_path(preview):
    assert preview.preview_path("") is None
    assert preview.preview_path(None) is None


# -- the projection --------------------------------------------------------------


def test_the_table_holds_a_128_TO_3_map_for_the_one_model_that_has_one(preview):
    entry = preview.PROJECTIONS[KEY]
    assert len(entry["factors"]) == 3
    assert all(len(row) == 128 for row in entry["factors"])
    assert len(entry["bias"]) == 3


def test_the_fitted_numbers_are_the_ones_that_were_MEASURED(preview):
    """A spot check against `factors.json`, so a re-typed or re-ordered constant
    is a failing test rather than a subtly wrong thumbnail. Channels 12..15 are
    the four with the largest positive weight in every output channel — the
    patchified brightness quartet — which is also the signature that would
    survive an accidental transpose, so a couple of small ones are here too."""
    entry = preview.PROJECTIONS[KEY]
    assert entry["factors"][0][13] == pytest.approx(0.03257348760962486, rel=1e-9)
    assert entry["factors"][1][32] == pytest.approx(-0.04735724627971649, rel=1e-9)
    assert entry["factors"][2][44] == pytest.approx(-0.016789274290204048, rel=1e-9)
    assert entry["bias"] == pytest.approx(
        [0.4698728024959564, 0.4328208565711975, 0.4053916037082672])


def test_an_all_zero_token_projects_to_the_BIAS(preview):
    """The constant term is the mean colour of the fit, and dropping it is the
    single easiest way to ship a thumbnail that is dark and plausible."""
    rgb = preview.project(_tokens(0.0, count=3), KEY)
    assert rgb.shape == (3, 3)
    assert rgb[0] == pytest.approx(preview.PROJECTIONS[KEY]["bias"], abs=1e-6)


def test_the_matrix_is_applied_in_the_ORIENTATION_it_was_fitted_in(preview):
    """`rgb = tokens @ factors.T + bias`, one row per token. A transpose here is
    a shape error only by luck — 3 and 128 differ, but the same numbers read
    the wrong way round would silently produce a colour field, and this asserts
    against the per-channel weights rather than against a picture."""
    entry = preview.PROJECTIONS[KEY]
    token = numpy.zeros((1, 128), dtype=numpy.float32)
    token[0, 13] = 2.0
    rgb = preview.project(token, KEY)
    expected = [entry["bias"][c] + 2.0 * entry["factors"][c][13] for c in range(3)]
    assert rgb[0] == pytest.approx(expected, abs=1e-6)


def test_the_projection_is_CLIPPED_to_a_displayable_range(preview):
    """An early estimate is far outside the fit's range and the arithmetic is
    unbounded; a page is handed a colour, not a float."""
    bright = preview.project(_tokens(500.0, count=1), KEY)
    dark = preview.project(_tokens(-500.0, count=1), KEY)
    assert bright.max() <= 1.0 and bright.min() >= 0.0
    assert dark.max() <= 1.0 and dark.min() >= 0.0
    assert bright.max() == pytest.approx(1.0)
    assert dark.min() == pytest.approx(0.0)


def test_a_model_with_no_entry_has_no_projection(preview):
    assert preview.project(_tokens(0.0), "AutoencoderKL") is None
    assert preview.project(_tokens(0.0), None) is None


# -- the denoised estimate -------------------------------------------------------


def test_the_estimate_is_recovered_from_TWO_latents_and_TWO_sigmas(preview):
    """`v = (x_next - x_prev) / (s_next - s_prev)`, `x1 = x_next - s_next * v`.
    With x going 0 -> 1 as sigma goes 1.0 -> 0.5, the velocity is -2 and the
    model's guess at the finished image is 2."""
    x1 = preview.denoised(numpy.zeros(4, dtype=numpy.float32),
                          numpy.ones(4, dtype=numpy.float32), 1.0, 0.5)
    assert x1 == pytest.approx(numpy.full(4, 2.0))


def test_the_estimate_at_sigma_zero_is_the_latent_itself(preview):
    """The last step lands on sigma 0, where the estimate degenerates to what
    the model just produced — so the final frame is the final image."""
    x_next = numpy.array([0.25, -0.5, 1.0, 0.0], dtype=numpy.float32)
    x1 = preview.denoised(numpy.zeros(4, dtype=numpy.float32), x_next, 0.3, 0.0)
    assert x1 == pytest.approx(x_next)


def test_two_identical_sigmas_have_no_estimate_rather_than_a_division_by_zero(preview):
    assert preview.denoised(numpy.zeros(4), numpy.ones(4), 0.5, 0.5) is None


# -- the sink --------------------------------------------------------------------


def test_the_FIRST_step_writes_nothing_because_it_has_no_predecessor(preview, tmp_path):
    """Step 1 has no previous latent, so there is no velocity and no estimate.
    That is correct rather than a gap: a first frame projected from the raw
    latent would be the noise this whole approach exists to avoid."""
    out = str(tmp_path / "a.preview.png")
    with preview.sink(out, KEY) as sink:
        sink.add(lambda: _tokens(0.0), sigma=0.9, grid=(4, 4))
        assert not os.path.exists(out)


def test_the_SECOND_step_writes_a_thumbnail_of_the_estimate(preview, tmp_path):
    """Verified legible from step 2 of 16 on a real render — and the pixels are
    checkable exactly, because a zero estimate is the bias colour."""
    out = str(tmp_path / "a.preview.png")
    bias = preview.PROJECTIONS[KEY]["bias"]
    expected = tuple(int(round(channel * 255)) for channel in bias)
    with preview.sink(out, KEY) as sink:
        sink.add(lambda: _tokens(0.0), sigma=1.0, grid=(4, 4))
        sink.add(lambda: _tokens(0.0), sigma=0.5, grid=(4, 4))
        assert _png_size(out) == (4, 4)
        assert set(_pixels(out)) == {expected}


def test_every_later_step_OVERWRITES_the_one_file(preview, tmp_path):
    """One file, not a filmstrip: a 100-step render would otherwise leave 100
    thumbnails in a directory the user browses, and the page's `<img>` would
    need to know which one is current."""
    out = str(tmp_path / "a.preview.png")
    with preview.sink(out, KEY) as sink:
        for step, sigma in enumerate([1.0, 0.8, 0.4, 0.0]):
            sink.add(lambda: _tokens(0.0), sigma=sigma, grid=(4, 4))
        assert os.listdir(tmp_path) == ["a.preview.png"]


def test_the_thumbnail_is_CAPPED_however_big_the_render_is(preview, tmp_path):
    """A 1024² render has a 64x64 token grid and a 2048² one has 128x128; the
    cost that was measured (68ms/step, ~3KB) is the cost of a 32x32 PNG, and the
    page blurs and upscales anyway."""
    out = str(tmp_path / "a.preview.png")
    with preview.sink(out, KEY) as sink:
        sink.add(lambda: _tokens(0.0, count=64 * 64), sigma=1.0, grid=(64, 64))
        sink.add(lambda: _tokens(0.0, count=64 * 64), sigma=0.5, grid=(64, 64))
        assert _png_size(out) == (preview.MAX_SIDE, preview.MAX_SIDE)


def test_a_NON_SQUARE_render_keeps_its_shape(preview, tmp_path):
    """The grid is `image side / 16` per axis, so a landscape render is a
    landscape thumbnail — an `<img>` sized to the final picture would otherwise
    stretch it."""
    out = str(tmp_path / "a.preview.png")
    with preview.sink(out, KEY) as sink:
        sink.add(lambda: _tokens(0.0, count=64 * 32), sigma=1.0, grid=(32, 64))
        sink.add(lambda: _tokens(0.0, count=64 * 32), sigma=0.5, grid=(32, 64))
        assert _png_size(out) == (preview.MAX_SIDE, preview.MAX_SIDE // 2)


def test_PACKED_tokens_and_an_UNPACKED_grid_produce_the_same_frame(preview, tmp_path):
    """diffusers hands the callback `(1, H*W, 128)` row-major packed tokens;
    mflux may hand `(B, 128, h, w)` already unpatchified. Both are the same
    picture and the sink takes either, because a runner reshaping it first is a
    second copy of the unpack rule."""
    rng = numpy.random.default_rng(0)
    grid = rng.standard_normal((1, 128, 4, 4)).astype(numpy.float32)
    packed = grid[0].transpose(1, 2, 0).reshape(1, 16, 128)
    written = []
    for name, latents in (("packed.preview.png", packed), ("grid.preview.png", grid)):
        out = str(tmp_path / name)
        with preview.sink(out, KEY) as sink:
            sink.add(lambda: numpy.zeros_like(latents), sigma=1.0, grid=(4, 4))
            sink.add(lambda: latents, sigma=0.0, grid=(4, 4))
            written.append(_pixels(out))
    assert written[0] == written[1]


def test_two_steps_at_the_SAME_sigma_leave_the_previous_frame_alone(preview, tmp_path):
    """No velocity, no estimate — and nothing written, rather than a frame
    computed from a division that did not happen."""
    out = str(tmp_path / "a.preview.png")
    with preview.sink(out, KEY) as sink:
        sink.add(lambda: _tokens(0.0), sigma=0.5, grid=(4, 4))
        sink.add(lambda: _tokens(0.0), sigma=0.5, grid=(4, 4))
        assert not os.path.exists(out)


# -- the no-op, which is what keeps this additive --------------------------------


def test_a_model_the_table_does_not_know_gets_NO_preview(preview, tmp_path):
    """A pipeline whose latent space nobody has fitted a matrix for has to
    render exactly as it did before this existed — no file, and no `if preview:`
    branch in either denoising loop."""
    out = str(tmp_path / "a.preview.png")
    with preview.sink(out, "AutoencoderKL") as sink:
        assert sink.wanted is False
        sink.add(lambda: _tokens(0.0), sigma=1.0, grid=(4, 4))
        sink.add(lambda: _tokens(0.0), sigma=0.5, grid=(4, 4))
    assert os.listdir(tmp_path) == []


def test_a_request_that_named_no_preview_file_gets_NO_preview(preview, tmp_path):
    with preview.sink(None, KEY) as sink:
        assert sink.wanted is False
        sink.add(lambda: _tokens(0.0), sigma=1.0, grid=(4, 4))
    assert os.listdir(tmp_path) == []


def test_a_no_op_sink_never_asks_for_the_LATENTS(preview, tmp_path):
    """The latents are handed over as a callable rather than as an array, and
    this is why: reading them costs a device->CPU sync per step, which is most
    of the 68ms the preview was measured at. A sink that is not writing must not
    charge the render for it."""
    def fetch():
        raise AssertionError("a no-op sink pulled the latents off the device")

    with preview.sink(None, KEY) as sink:
        sink.add(fetch, sigma=1.0, grid=(4, 4))
    with preview.sink(str(tmp_path / "a.preview.png"), "nope") as sink:
        sink.add(fetch, sigma=1.0, grid=(4, 4))


# -- atomicity -------------------------------------------------------------------


def test_a_frame_lands_by_REPLACE_from_a_temp_file_in_the_SAME_directory(
        preview, tmp_path, monkeypatch):
    """The page is reading this file through `/api/fs/raw` while the worker
    rewrites it, so a frame written in place is a torn read. Same directory
    because `os.replace` is only atomic within one filesystem."""
    out = str(tmp_path / "a.preview.png")
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append((src, dst))
        assert os.path.dirname(src) == os.path.dirname(dst), (src, dst)
        assert src != dst
        # Whatever a reader holds open at this instant is a COMPLETE frame:
        # either nothing, or the previous one.
        if os.path.exists(dst):
            _png_size(dst)
        real_replace(src, dst)

    monkeypatch.setattr(preview.os, "replace", spy)
    with preview.sink(out, KEY) as sink:
        for sigma in (1.0, 0.8, 0.4):
            sink.add(lambda: _tokens(0.0), sigma=sigma, grid=(4, 4))
    assert [dst for _, dst in seen] == [out, out]


def test_a_reader_ONLY_ever_sees_a_whole_png(preview, tmp_path):
    """The observable half of the rule above: every byte that has ever been at
    this path parses as a PNG of the expected size."""
    out = str(tmp_path / "a.preview.png")
    with preview.sink(out, KEY) as sink:
        for sigma in (1.0, 0.9, 0.7, 0.3, 0.0):
            sink.add(lambda: _tokens(0.0), sigma=sigma, grid=(4, 4))
            if os.path.exists(out):
                assert _png_size(out) == (4, 4)


# -- the lifecycle, and where it diverges from partial.Sink ----------------------


def _render(preview, out, raising=None):
    with preview.sink(out, KEY) as sink:
        sink.add(lambda: _tokens(0.0), sigma=1.0, grid=(4, 4))
        sink.add(lambda: _tokens(0.0), sigma=0.5, grid=(4, 4))
        assert os.path.exists(out)
        if raising is not None:
            raise raising


def test_the_preview_is_GONE_once_the_real_image_lands(preview, tmp_path):
    """A clean exit means the PNG the request named has been written, so the
    preview is duplicate bytes at a lower resolution."""
    out = str(tmp_path / "a.preview.png")
    _render(preview, out)
    assert os.listdir(tmp_path) == []


def test_the_preview_is_GONE_when_the_render_is_cancelled(preview, tmp_path):
    out = str(tmp_path / "a.preview.png")
    with pytest.raises(Cancelled):
        _render(preview, out, raising=Cancelled())
    assert os.listdir(tmp_path) == []


def test_the_preview_is_GONE_when_the_render_FAILS_too(preview, tmp_path):
    """**The divergence from `partial.Sink`, which keeps its file on an error.**
    A transcript that died at minute 80 of 90 has 80 minutes of words in it and
    that file is the only salvage there is. A render that died at step 12 of 16
    has a 32x32 blur of a picture that will never exist — not salvage, just a
    file in `ai/images/` that no row explains and nothing will ever clean up."""
    out = str(tmp_path / "a.preview.png")
    with pytest.raises(RuntimeError):
        _render(preview, out, raising=RuntimeError("the render exploded"))
    assert os.listdir(tmp_path) == []


def test_nothing_is_left_behind_when_a_frame_fails_MID_WRITE(preview, tmp_path,
                                                             monkeypatch):
    """The temp file is the one thing that can outlive its writer. It is in the
    directory the user browses, so it goes with everything else."""
    out = str(tmp_path / "a.preview.png")

    def boom(src, dst):
        raise OSError("no space left on device")

    monkeypatch.setattr(preview.os, "replace", boom)
    with pytest.raises(OSError):
        with preview.sink(out, KEY) as sink:
            sink.add(lambda: _tokens(0.0), sigma=1.0, grid=(4, 4))
            sink.add(lambda: _tokens(0.0), sigma=0.5, grid=(4, 4))
    assert os.listdir(tmp_path) == []


def test_discarding_a_preview_that_was_never_written_is_not_an_error(preview, tmp_path):
    """A render cancelled on its first step arrives here having written
    nothing, and a crash in the teardown would report a finished render as a
    failed one — the trade `partial.Sink.discard` refuses to take."""
    out = str(tmp_path / "a.preview.png")
    with preview.sink(out, KEY):
        pass
    assert os.listdir(tmp_path) == []


# -- one implementation, two engines ---------------------------------------------


def test_the_SERVER_reaches_the_same_module_through_the_package(preview):
    """Two loaders, one module — the route derives the path it advertises from
    `preview_path`, so a second copy under a second module name would let the
    route and the worker name different files."""
    from fused_render.ai.runners import preview as packaged

    assert packaged.preview_path("/x/y.png") == preview.preview_path("/x/y.png")
    assert packaged.PROJECTIONS.keys() == preview.PROJECTIONS.keys()


def test_the_previewer_needs_NO_third_party_import_to_be_READ(preview):
    """The server imports this file to derive the path it advertises, on an
    interpreter where neither runner venv's packages exist. numpy and Pillow are
    both present in both runner venvs and both are used — inside the functions
    that need them, the way `diarize.py` keeps sherpa out of module scope."""
    source = open(PREVIEW_PATH, encoding="utf-8").read()
    assert "import fused_render" not in source
    for line in source.splitlines():
        assert not line.startswith("import numpy"), line
        assert not line.startswith("from PIL"), line
        assert not line.startswith("import PIL"), line
