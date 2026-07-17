"""Tests for the pinned-view store (fused_render/pin_store.py, SPEC §25 PV-1).

Pure round-trip and corruption-tolerance tests: pin_store is the only
CI-testable slice of the pinned-view feature (the popover itself is AppKit,
PV-7). A bad pin.json must read back as "no pin", never raise — the recovery
path is just pinning again from the menu.
"""

import os

from fused_render import pin_store


def test_load_missing_is_none(tmp_path):
    assert pin_store.load_pin(str(tmp_path)) is None


def test_save_then_load_roundtrip(tmp_path):
    pin_store.save_pin(str(tmp_path), "/some/file.html")
    assert pin_store.load_pin(str(tmp_path)) == "/some/file.html"


def test_save_creates_dir(tmp_path):
    target = str(tmp_path / "nested" / "dir")
    pin_store.save_pin(target, "/x.parquet")
    assert pin_store.load_pin(target) == "/x.parquet"


def test_save_overwrites(tmp_path):
    pin_store.save_pin(str(tmp_path), "/a.html")
    pin_store.save_pin(str(tmp_path), "/b.html")
    assert pin_store.load_pin(str(tmp_path)) == "/b.html"


def test_clear_removes(tmp_path):
    pin_store.save_pin(str(tmp_path), "/a.html")
    pin_store.clear_pin(str(tmp_path))
    assert pin_store.load_pin(str(tmp_path)) is None
    assert not os.path.exists(str(tmp_path / pin_store.PIN_FILENAME))


def test_clear_when_missing_is_noop(tmp_path):
    pin_store.clear_pin(str(tmp_path))  # must not raise


def test_corrupt_json_is_none(tmp_path):
    (tmp_path / pin_store.PIN_FILENAME).write_text("{not json")
    assert pin_store.load_pin(str(tmp_path)) is None


def test_wrong_shape_is_none(tmp_path):
    (tmp_path / pin_store.PIN_FILENAME).write_text('["list", "not", "dict"]')
    assert pin_store.load_pin(str(tmp_path)) is None


def test_empty_or_nonstring_path_is_none(tmp_path):
    (tmp_path / pin_store.PIN_FILENAME).write_text('{"path": ""}')
    assert pin_store.load_pin(str(tmp_path)) is None
    (tmp_path / pin_store.PIN_FILENAME).write_text('{"path": 42}')
    assert pin_store.load_pin(str(tmp_path)) is None


def test_size_roundtrip(tmp_path):
    assert pin_store.load_size(str(tmp_path)) is None
    pin_store.save_size(str(tmp_path), 500, 430)
    assert pin_store.load_size(str(tmp_path)) == (500, 430)


def test_size_survives_repin_and_unpin(tmp_path):
    pin_store.save_pin(str(tmp_path), "/a.html")
    pin_store.save_size(str(tmp_path), 500, 430)
    pin_store.save_pin(str(tmp_path), "/b.html")  # re-pin keeps size
    assert pin_store.load_size(str(tmp_path)) == (500, 430)
    pin_store.clear_pin(str(tmp_path))  # unpin keeps size, drops path
    assert pin_store.load_pin(str(tmp_path)) is None
    assert pin_store.load_size(str(tmp_path)) == (500, 430)


def test_bad_size_is_none(tmp_path):
    (tmp_path / pin_store.PIN_FILENAME).write_text('{"size": [0, -3]}')
    assert pin_store.load_size(str(tmp_path)) is None
    (tmp_path / pin_store.PIN_FILENAME).write_text('{"size": "big"}')
    assert pin_store.load_size(str(tmp_path)) is None
