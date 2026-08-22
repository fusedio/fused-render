"""`embed_common.py` — the request shape and the arithmetic both embedding
runners share (SPEC §40).

Plain functions over plain data, so this is driven directly rather than
through either runner's own `sys.path` dance (`tests/test_ai_mlx_worker.py`'s
style) — `embed_common` has no engine of its own to fake.
"""
import sys

import re

import pytest
from PIL import Image

from fused_render.ai.runners import embed_common


# -- request_kind ---------------------------------------------------------------


def test_texts_alone_is_accepted():
    assert embed_common.request_kind({"texts": ["a", "b"]}) == ("texts", ["a", "b"])


def test_paths_alone_is_accepted():
    assert embed_common.request_kind({"paths": ["/a.png"]}) == ("paths", ["/a.png"])


def test_neither_is_refused():
    with pytest.raises(ValueError, match="texts.*paths"):
        embed_common.request_kind({})


def test_an_empty_body_is_the_same_refusal_as_neither():
    """The router's own "no body" case: `{}` and "neither key present" are the
    identical shape, so there is exactly one code path refusing both."""
    with pytest.raises(ValueError):
        embed_common.request_kind({})


def test_both_keys_is_refused():
    with pytest.raises(ValueError, match="not both"):
        embed_common.request_kind({"texts": ["a"], "paths": ["/a.png"]})


def test_an_empty_list_is_refused():
    with pytest.raises(ValueError):
        embed_common.request_kind({"texts": []})


def test_over_max_items_is_refused():
    with pytest.raises(ValueError, match="64"):
        embed_common.request_kind({"texts": ["x"] * (embed_common.MAX_ITEMS + 1)})


def test_exactly_max_items_is_accepted():
    kind, items = embed_common.request_kind({"texts": ["x"] * embed_common.MAX_ITEMS})
    assert kind == "texts"
    assert len(items) == embed_common.MAX_ITEMS


def test_a_non_string_item_is_refused():
    with pytest.raises(ValueError, match=r"texts\[1\]"):
        embed_common.request_kind({"texts": ["fine", 42]})


def test_an_empty_string_item_is_refused():
    with pytest.raises(ValueError, match=r"paths\[0\]"):
        embed_common.request_kind({"paths": [""]})


# -- unit_normalize ---------------------------------------------------------------


def test_every_row_lands_at_unit_length():
    vectors = embed_common.unit_normalize([[3.0, 4.0], [1.0, 0.0], [0.0, -2.0]])
    for row in vectors:
        norm = sum(v * v for v in row) ** 0.5
        assert abs(norm - 1.0) < 1e-9


def test_direction_is_preserved():
    vectors = embed_common.unit_normalize([[3.0, 4.0]])
    assert vectors[0][0] == pytest.approx(0.6)
    assert vectors[0][1] == pytest.approx(0.8)


def test_a_zero_vector_is_left_as_zero_not_divided():
    """A real model never emits one; a mocked model in a test might."""
    vectors = embed_common.unit_normalize([[0.0, 0.0, 0.0]])
    assert vectors == [[0.0, 0.0, 0.0]]


def test_the_result_is_plain_python_floats():
    vectors = embed_common.unit_normalize([[3, 4]])
    assert all(isinstance(v, float) for v in vectors[0])


# -- open_image ---------------------------------------------------------------


def test_open_image_reads_a_real_picture_as_rgb(tmp_path):
    path = tmp_path / "pic.png"
    Image.new("RGBA", (4, 4), (10, 20, 30, 255)).save(path)
    image = embed_common.open_image(str(path))
    assert image.mode == "RGB"
    assert image.size == (4, 4)


def test_open_image_names_a_missing_file(tmp_path):
    missing = tmp_path / "nope.png"
    with pytest.raises(ValueError, match=re.escape(str(missing))):
        embed_common.open_image(str(missing))


def test_open_image_names_a_directory(tmp_path):
    with pytest.raises(ValueError, match=re.escape(str(tmp_path))):
        embed_common.open_image(str(tmp_path))


def test_open_image_refuses_a_non_image_file(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("this is not a picture")
    with pytest.raises(ValueError, match="not a readable image"):
        embed_common.open_image(str(path))


def test_a_truncated_image_is_decoded_rather_than_refused(tmp_path):
    """A half-written JPEG must not take a batch of 64 down with it.

    Photo libraries are full of them — interrupted downloads, a messaging app's
    cache — and PIL's default is `OSError: image file is truncated`. Reported
    from a real run over a WhatsApp cache directory, where one such file aborted
    the whole embed call and stopped indexing on a file the user cannot act on.
    """
    from PIL import Image

    whole = tmp_path / "whole.jpg"
    Image.new("RGB", (64, 64), (10, 120, 200)).save(whole, quality=95)
    cut = tmp_path / "cut.jpg"
    data = whole.read_bytes()
    cut.write_bytes(data[:-2])                      # lose the tail, as reported

    image = embed_common.open_image(str(cut))
    assert image.mode == "RGB"
    assert image.size == (64, 64)


def test_heic_is_registered_so_a_photo_library_is_readable(monkeypatch):
    """HEIC is the iPhone default, and plain Pillow cannot open it.

    Registration is what makes `paths` work on photographs rather than only on
    screenshots — measured on a real library, the first `.HEIC` came back "is
    not a readable image" before this. Asserted through the module's own flag
    and a stubbed opener, so the test does not need pillow-heif installed here.
    """
    calls = []
    fake = type(sys)("pillow_heif")
    fake.register_heif_opener = lambda: calls.append(1)
    monkeypatch.setitem(sys.modules, "pillow_heif", fake)
    monkeypatch.setattr(embed_common, "_heif_registered", False)

    embed_common._register_heif()
    embed_common._register_heif()          # once per process, not per image
    assert calls == [1]


def test_a_missing_pillow_heif_does_not_break_other_formats(monkeypatch, tmp_path):
    """An engine whose venv predates the dependency must still embed PNGs."""
    from PIL import Image

    monkeypatch.setitem(sys.modules, "pillow_heif", None)   # import raises
    monkeypatch.setattr(embed_common, "_heif_registered", False)
    path = tmp_path / "ok.png"
    Image.new("RGB", (8, 8), (4, 5, 6)).save(path)

    assert embed_common.open_image(str(path)).mode == "RGB"
