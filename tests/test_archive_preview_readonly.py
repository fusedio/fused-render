"""Tests for the archive readers' preview extraction (zip + tar reader.py).

A previewed member is a throwaway temp copy — the normal template pipeline
opens it as if it were the real file, so without a marker an rw template
happily "saves" edits into the temp copy and they never reach the archive.
The readers therefore land every preview copy read-only (0444, written via
tmp + os.replace so concurrent previews never observe a half-written or
permission-flapping file), which flows through the whole RO contract (SPEC
13.5 RO-7) untouched: stat.writable goes false, /api/fs/write refuses, and
every template's writer gate (os.access W_OK) holds. Deliberate
`extract`/`extract_all` output keeps the original semantics — writable, and
failing loudly (EACCES) on a write-protected existing target instead of
silently replacing it.
"""
import gzip
import importlib.util
import os
import tarfile
import zipfile

import pytest


def _load(template):
    path = os.path.join(os.path.dirname(__file__), "..", "fused_render",
                        "templates", template, "reader.py")
    spec = importlib.util.spec_from_file_location(f"{template}_reader", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zip_reader = _load("zip")
tar_reader = _load("tar")


@pytest.fixture(autouse=True)
def temp_root(tmp_path, monkeypatch):
    """Point the preview roots' tempdir at the test's tmp_path so tests never
    touch (or collide in) the real shared /tmp/fused-render-{zip,tar}."""
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(zip_reader.tempfile, "gettempdir", lambda: str(root))
    return root


def _zip(tmp_path, content="hello"):
    p = tmp_path / "sample.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("notes.txt", content)
        zf.writestr("sub/data.csv", "a,b\n1,2\n")
    return str(p)


def _tar(tmp_path, content="hello"):
    p = tmp_path / "sample.tar.gz"
    member = tmp_path / "notes.txt"
    member.write_text(content)
    with tarfile.open(p, "w:gz") as tf:
        tf.add(member, arcname="notes.txt")
    return str(p)


ARCHIVES = [(zip_reader, _zip), (tar_reader, _tar)]

# os.access always says yes for root, so the chmod-based gates can't trip.
skip_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="read-only bits are ignored when running as root")


@skip_root
@pytest.mark.parametrize("reader,make", ARCHIVES, ids=["zip", "tar"])
def test_preview_copy_is_read_only(tmp_path, reader, make):
    res = reader.main(make(tmp_path), "preview", "notes.txt")
    path = res["path"]
    with open(path) as f:
        assert f.read() == "hello"
    assert not os.access(path, os.W_OK)


@skip_root
@pytest.mark.parametrize("reader,make", ARCHIVES, ids=["zip", "tar"])
def test_preview_overwrites_stale_read_only_copy(tmp_path, reader, make):
    first = reader.main(make(tmp_path), "preview", "notes.txt")["path"]
    assert not os.access(first, os.W_OK)
    # The archive changes; a re-preview must replace the read-only copy.
    second = reader.main(make(tmp_path, content="hello v2"),
                         "preview", "notes.txt")["path"]
    assert second == first
    with open(second) as f:
        assert f.read() == "hello v2"
    assert not os.access(second, os.W_OK)


@pytest.mark.parametrize("reader,make", ARCHIVES, ids=["zip", "tar"])
def test_extract_output_stays_writable(tmp_path, reader, make):
    res = reader.main(make(tmp_path), "extract", "notes.txt")
    assert os.access(res["path"], os.W_OK)


@skip_root
@pytest.mark.parametrize("reader,make", ARCHIVES, ids=["zip", "tar"])
def test_extract_refuses_write_protected_target(tmp_path, reader, make):
    # The preview chmod must NOT leak into deliberate extraction: a file the
    # user write-protected at the extract destination still fails loudly
    # instead of being silently unlinked and replaced.
    archive = make(tmp_path)
    out = reader.main(archive, "extract", "notes.txt")["path"]
    os.chmod(out, 0o444)
    try:
        with pytest.raises(PermissionError):
            reader.main(archive, "extract", "notes.txt")
    finally:
        os.chmod(out, 0o644)  # so tmp_path cleanup works


def test_zip_extract_all_output_stays_writable(tmp_path):
    res = zip_reader.main(_zip(tmp_path), "extract_all")
    checked = 0
    for base, _, files in os.walk(res["dest"]):
        for name in files:
            assert os.access(os.path.join(base, name), os.W_OK)
            checked += 1
    assert checked == 2


@skip_root
def test_tar_single_compressed_preview_is_read_only(tmp_path):
    p = tmp_path / "notes.json.gz"
    with gzip.open(p, "wt") as f:
        f.write("{}")
    res = tar_reader.main(str(p), "preview")
    assert not os.access(res["path"], os.W_OK)
