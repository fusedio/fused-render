"""The image ladder is written TWICE. This pins the two copies to one answer.

`fused_render/server/image_convert.py` transcodes an upload for the New task
modal; `fused_render/templates/claude/agent.py`'s `image_to_png` action
transcodes a chat attachment (D614). Same job, same reason — a picture neither
the browser nor the agent can decode — and deliberately two implementations,
because SPEC PY-15 / D166 forbid a template importing `fused_render`: the claude
agent runs as a subprocess (absent from `executor.INPROCESS_HELPERS`), PY-6a
retired the `sys.path` bootstrap that once made such an import resolve, and the
fused local backend strips `PYTHONPATH` from its children. An `import` there
would work in this checkout and fail — or worse, silently take a fallback —
everywhere else, which is the silent-divergence class that rule exists to end.
`templates/shared/` is not a home for it either: that layer is stdlib-only and
this needs Pillow.

So: written twice, tested once, the same shape `_pane_file`, `_ann_notes` and
`_app_dir_for` already have. What is pinned here is what would actually cost a
user if it drifted — the four numbers, and the BYTES out of each branch of the
ladder (below the cap, over the edge, over the byte budget). Byte-identical
rather than merely similar: the two run the same Pillow encoder over the same
pixels, so anything less would let a changed mode coercion or a dropped
`optimize=` through.

NOT pinned here, and deliberately: the `sips` fallback (no HEIC fixture on a
non-darwin runner — `tests/test_claude_shots.py` covers it end to end where it
exists) and the containment check, which is the ONE half that must never be
mirrored — `_in_shots` authorises the path and `image_convert` authorises
nothing, by design.
"""
import importlib.util
import os
import shutil

import pytest

from fused_render.server import image_convert

AGENT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "claude", "agent.py")


def _pillow():
    pytest.importorskip("PIL", reason="pillow is a bundled extra")
    from PIL import Image
    return Image


@pytest.fixture
def agent():
    """agent.py loaded the way the worker runs it: by path, as a standalone
    module, never as part of the package."""
    spec = importlib.util.spec_from_file_location("parity_agent", AGENT_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _both(agent, tmp_path, monkeypatch, src_name, save):
    """Run one picture through both ladders and return `(agent_out, shared_out)`.

    Each gets its own directory: the agent's copy is named for it by
    `_image_to_png` (the stem, a sibling of the original) while the shared one
    takes the `dest_base` its caller chose, and the point of this test is the
    PIXELS AND BYTES, not the two callers' naming. Nothing here reaches across
    into the other's directory, so a name collision cannot make them agree."""
    shots = tmp_path / "shots"
    shots.mkdir()
    monkeypatch.setattr(agent, "SHOTS", str(shots))
    src = shots / src_name
    save(src)

    mine = tmp_path / "server"
    mine.mkdir()
    shutil.copy2(str(src), str(mine / src_name))

    got = agent.main(action="image_to_png", path=str(src))
    shared = image_convert.transcode(
        str(mine / src_name),
        str(mine / (os.path.splitext(src_name)[0] + "-view")))
    assert "error" not in got, got
    assert "error" not in shared, shared
    return got, shared


def _assert_same(got, shared):
    """Same picture, same format, same bytes — everything but the path, which is
    each caller's to choose."""
    Image = _pillow()
    for key in ("width", "height", "bytes", "source_w", "source_h"):
        assert got[key] == shared[key], (key, got, shared)
    assert os.path.splitext(got["path"])[1] == \
        os.path.splitext(shared["path"])[1], (got["path"], shared["path"])
    assert Image.open(got["path"]).format == Image.open(shared["path"]).format
    with open(got["path"], "rb") as a, open(shared["path"], "rb") as b:
        assert a.read() == b.read(), "same encoder, same pixels, same bytes"
    assert got["bytes"] == os.path.getsize(got["path"])
    assert shared["bytes"] == os.path.getsize(shared["path"])


def test_the_two_ladders_share_all_four_numbers(agent):
    """The numbers are the whole ladder, and a silent split in any of them is a
    conversion that came back bigger, sharper or slower on one surface than the
    other for no reason a user could see. Spelled with the literals as well as
    against each other, so "both changed together, by accident" still fails."""
    mod = agent
    assert mod.SHOT_PNG_EDGE == image_convert.PNG_EDGE == 1600
    assert mod.SHOT_PNG_MAX_BYTES == image_convert.PNG_MAX_BYTES == 4 * 1024 * 1024
    assert mod.SHOT_JPEG_QUALITY == image_convert.JPEG_QUALITY == (90, 80, 70, 60)
    assert mod.SHOT_SIPS_TIMEOUT == image_convert.SIPS_TIMEOUT == 20


def test_a_small_png_comes_out_of_both_identically(agent, tmp_path, monkeypatch):
    """The do-nothing branch: already a PNG, already under both caps. It still
    goes through the whole ladder (decode, mode coercion, re-encode), so it is
    the one that catches a changed `optimize=` or a mode rule that stopped
    agreeing."""
    Image = _pillow()
    got, shared = _both(
        agent, tmp_path, monkeypatch, "20260828-small.png",
        lambda p: Image.new("RGB", (40, 30), (10, 120, 200)).save(
            str(p), format="PNG"))
    assert (got["source_w"], got["source_h"]) == (40, 30)
    assert (got["width"], got["height"]) == (40, 30), "small enough already"
    assert got["path"].endswith(".png")
    _assert_same(got, shared)


def test_an_oversize_tiff_is_capped_to_the_same_edge_by_both(agent, tmp_path,
                                                             monkeypatch):
    """The downscale branch, on the format the D614 bug was reported with: 2400px
    of TIFF has to come back as 1600px of PNG from BOTH, with the aspect kept.
    A split here is the app's own screenshots and an upload disagreeing about how
    big a picture the agent gets."""
    Image = _pillow()
    got, shared = _both(
        agent, tmp_path, monkeypatch, "20260828-big.tif",
        lambda p: Image.new("RGB", (2400, 1200), (200, 60, 40)).save(
            str(p), format="TIFF"))
    assert (got["source_w"], got["source_h"]) == (2400, 1200)
    assert (got["width"], got["height"]) == (1600, 800)
    assert max(got["width"], got["height"]) == image_convert.PNG_EDGE
    assert got["path"].endswith(".png")
    _assert_same(got, shared)


def test_both_trip_the_jpeg_ladder_at_the_same_point(agent, tmp_path,
                                                     monkeypatch):
    """The byte-budget branch: a PNG over the cap becomes a JPEG, and the two
    have to agree on WHEN that happens and on which rung of 90→60 they stop at.

    The cap is monkeypatched on both rather than fed a >4 MB fixture — the real
    numbers are pinned by the constants test above, and encoding 7 MB of noise
    twice would buy nothing but seconds. Noise rather than a flat fill because a
    flat PNG compresses to nothing and would never reach the ladder."""
    Image = _pillow()
    noise = Image.frombytes("RGB", (240, 180), os.urandom(240 * 180 * 3))
    monkeypatch.setattr(agent, "SHOT_PNG_MAX_BYTES", 4096)
    monkeypatch.setattr(image_convert, "PNG_MAX_BYTES", 4096)
    got, shared = _both(agent, tmp_path, monkeypatch, "20260828-noise.png",
                        lambda p: noise.save(str(p), format="PNG"))
    assert got["path"].endswith(".jpg"), "the PNG missed the budget"
    assert (got["width"], got["height"]) == (240, 180), "quality before pixels"
    _assert_same(got, shared)


def test_the_shared_module_authorises_nothing(tmp_path):
    """The half that must NOT be mirrored. `agent.py` refuses a path outside its
    shots dir (`_in_shots`); `image_convert` has no such notion and must not grow
    one — its caller is an upload endpoint whose staging directory is its own
    business. Pinned so a later "let's share the containment check too" has to
    argue with a failing test."""
    assert not hasattr(image_convert, "_in_shots")
    assert not any("shots" in name.lower() for name in dir(image_convert))
