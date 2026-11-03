"""scripts/dmg_background.py renders the DMG window background build_dmg.sh
hands to dmgbuild. The DMG itself cannot be built off macOS; the image can,
so check the contract build_dmg.sh and the dmgbuild settings rely on: both
scale files, at the sizes the settings' window_rect assumes, and the
caption flag."""
import importlib.util
import os

import pytest

PIL = pytest.importorskip("PIL")

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_REPO, "scripts", "dmg_background.py")


def _load():
    """Import the script by path: it is not a package module."""
    spec = importlib.util.spec_from_file_location("dmg_background", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_writes_both_scales_at_the_window_size(tmp_path):
    """dmgbuild pairs `name.png` with `name@2x.png`, at the window's size."""
    mod = _load()
    out = tmp_path / "bg.png"
    mod.main([str(out)])
    one = PIL.Image.open(out)
    two = PIL.Image.open(tmp_path / "bg@2x.png")
    assert one.size == (mod.WIDTH, mod.HEIGHT) == (760, 496)
    assert two.size == (1520, 992)
    # dmgbuild pairs `name.png` with `name@2x.png`; any other name is not found.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bg.png", "bg@2x.png"]


def test_sparkles_stay_clear_of_the_icons_and_labels():
    """The icon centers and size are build_dmg.sh's dmgbuild settings; a
    sparkle under an icon or its label reads as a smudge."""
    mod = _load()
    icon_boxes = [(225, 218), (535, 218)]
    half = 160 / 2 + 6
    for cx, cy, r, _rot, _color in mod.SPARKLES:
        for ix, iy in icon_boxes:
            assert not (abs(cx - ix) < half + r and abs(cy - iy) < half + r), (cx, cy)
            assert not (abs(cx - ix) < 70 + r and 296 < cy + r and cy - r < 340), (cx, cy)  # the label row
        assert r < cx < mod.WIDTH - r and r < cy < mod.HEIGHT - r


def test_caption_is_optional(tmp_path):
    """`--caption ''` renders the canvas with no text."""
    mod = _load()
    with_caption = mod.render("Drag Fused Render into Applications")
    without = mod.render("")
    assert with_caption.tobytes() != without.tobytes()


def test_refuses_a_non_png_output(tmp_path):
    """Any other extension would break dmgbuild's @2x pairing."""
    mod = _load()
    with pytest.raises(SystemExit):
        mod.main([str(tmp_path / "bg.tiff")])
