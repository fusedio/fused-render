"""Tests for the zip template reader's preview extraction (reader.py).

A previewed member is a throwaway temp copy — the normal template pipeline
opens it as if it were the real file, so without a marker an rw template
happily "saves" edits into the temp copy and they never reach the archive.
The reader therefore chmods every preview copy read-only, which flows through
the whole RO contract (SPEC 13.5) untouched: stat.writable goes false,
/api/fs/write refuses, and every template's writer gate (os.access W_OK)
holds. Deliberate `extract`/`extract_all` output stays writable — that is a
user export, not a preview.
"""
import importlib.util
import os
import zipfile

import pytest


def _load():
    path = os.path.join(os.path.dirname(__file__), "..", "fused_render",
                        "templates", "zip", "reader.py")
    spec = importlib.util.spec_from_file_location("zip_reader", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


reader = _load()


@pytest.fixture(autouse=True)
def temp_root(tmp_path, monkeypatch):
    """Point the preview root's tempdir at the test's tmp_path so tests never
    touch (or collide in) the real shared /tmp/fused-render-zip."""
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(reader.tempfile, "gettempdir", lambda: str(root))
    return root


def _archive(tmp_path, content="hello"):
    p = tmp_path / "sample.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("notes.txt", content)
        zf.writestr("sub/data.csv", "a,b\n1,2\n")
    return str(p)


def test_preview_copy_is_read_only(tmp_path):
    res = reader.main(_archive(tmp_path), "preview", "notes.txt")
    path = res["path"]
    with open(path) as f:
        assert f.read() == "hello"
    assert not os.access(path, os.W_OK)


def test_preview_overwrites_stale_read_only_copy(tmp_path):
    archive = _archive(tmp_path)
    first = reader.main(archive, "preview", "notes.txt")["path"]
    assert not os.access(first, os.W_OK)
    # The archive changes; a re-preview must replace the read-only copy.
    archive = _archive(tmp_path, content="hello v2")
    second = reader.main(archive, "preview", "notes.txt")["path"]
    assert second == first
    with open(second) as f:
        assert f.read() == "hello v2"
    assert not os.access(second, os.W_OK)


def test_extract_output_stays_writable(tmp_path):
    res = reader.main(_archive(tmp_path), "extract", "notes.txt")
    assert os.access(res["path"], os.W_OK)


def test_extract_all_output_stays_writable(tmp_path):
    res = reader.main(_archive(tmp_path), "extract_all")
    dest = res["dest"]
    for base, _, files in os.walk(dest):
        for name in files:
            assert os.access(os.path.join(base, name), os.W_OK)
