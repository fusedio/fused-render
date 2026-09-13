"""The .fused v2 opaque container (appfile_container): a scanner sees an
unknown binary, never an archive; the reader trusts nothing the header or
index declares."""

import hashlib
import json
import struct
import zlib

import pytest

from fused_render import appfile_container as c

CAPS = {"max_entries": 100, "max_entry_bytes": 1024 * 1024, "max_total_bytes": 4 * 1024 * 1024}


def build(tmp_path, members, index=None, name="x.fused"):
    out = tmp_path / name
    c.write(str(out), index or {"name": "demo", "entry": "index.html"}, members)
    return out


def raw_container(index: dict, data: bytes = b"", isize=None, icsize=None) -> bytes:
    raw = json.dumps(index).encode()
    cindex = zlib.compress(raw)
    return (
        c._HEADER.pack(c.MAGIC, c.VERSION, 0,
                       len(raw) if isize is None else isize,
                       len(cindex) if icsize is None else icsize)
        + cindex + data
    )


def blob(payload: bytes):
    comp = zlib.compress(payload)
    return comp, {"size": len(payload), "csize": len(comp),
                  "sha256": hashlib.sha256(payload).hexdigest()}


def test_round_trip_and_not_a_zip(tmp_path):
    src = tmp_path / "data.py"
    src.write_bytes(b"print(1)\n" * 1000)
    out = build(tmp_path, [("index.html", b"<html>hi</html>"), ("lib/data.py", str(src))])
    head = out.read_bytes()
    assert head.startswith(b"FUSEDAPP")
    assert b"PK\x03\x04" not in head and b"<html>" not in head  # nothing sniffable in clear
    idx = c.read_index(str(out), **CAPS)
    assert idx["fused_app_file"] == 2 and idx["entry"] == "index.html"
    assert [f["path"] for f in idx["files"]] == ["index.html", "lib/data.py"]
    assert c.read_member(str(out), idx, "index.html", 100) == b"<html>hi</html>"
    assert c.read_member(str(out), idx, "nope", 100) is None
    dest = tmp_path / "out"
    c.extract(str(out), idx, str(dest))
    assert (dest / "lib" / "data.py").read_bytes() == src.read_bytes()


def test_writer_refuses_unsafe_paths(tmp_path):
    for bad in ["../x", "/abs", "a\\b", "a//b", "", "./a"]:
        with pytest.raises(c.ContainerError):
            build(tmp_path, [(bad, b"x")])


def test_reader_rejects_bad_magic_and_future_version(tmp_path):
    f = tmp_path / "a.fused"
    f.write_bytes(b"PK\x03\x04" + b"\x00" * 40)
    assert not c.is_container(str(f))
    with pytest.raises(c.ContainerError, match="bad magic"):
        c.read_index(str(f), **CAPS)
    f.write_bytes(struct.pack("<8sHHII", c.MAGIC, 99, 0, 2, 2) + b"xx")
    with pytest.raises(c.ContainerError, match="version 99"):
        c.read_index(str(f), **CAPS)


def test_index_is_capped_before_inflate(tmp_path):
    # A header declaring a huge index, and a small compressed index that
    # inflates past the cap, both stop before the bytes are materialized.
    f = tmp_path / "a.fused"
    f.write_bytes(raw_container({"fused_app_file": 2, "files": []}, isize=c.MAX_INDEX_BYTES + 1))
    with pytest.raises(c.ContainerError, match="too large"):
        c.read_index(str(f), **CAPS)
    bomb = zlib.compress(b" " * (c.MAX_INDEX_BYTES + 4096))
    f.write_bytes(struct.pack("<8sHHII", c.MAGIC, 2, 0, 100, len(bomb)) + bomb)
    with pytest.raises(c.ContainerError, match="declared size"):
        c.read_index(str(f), **CAPS)


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/x", "a\\..\\b", "a/./b", "a\x00b"])
def test_index_rejects_traversal_paths(tmp_path, path):
    comp, meta = blob(b"x")
    f = tmp_path / "a.fused"
    f.write_bytes(raw_container(
        {"fused_app_file": 2, "name": "e", "entry": "i", "files": [{"path": path, "offset": 0, **meta}]},
        comp))
    with pytest.raises(c.ContainerError, match="rejected"):
        c.read_index(str(f), **CAPS)


def test_index_rejects_file_dir_conflicts_and_duplicates(tmp_path):
    comp, meta = blob(b"x")
    for paths in (["a", "a/b"], ["a/b", "a"], ["a", "a"]):
        f = tmp_path / "a.fused"
        f.write_bytes(raw_container(
            {"fused_app_file": 2, "files": [{"path": p, "offset": 0, **meta} for p in paths]},
            comp))
        with pytest.raises(c.ContainerError, match="duplicate|both a file"):
            c.read_index(str(f), **CAPS)


def test_index_enforces_caps_and_bounds(tmp_path):
    comp, meta = blob(b"x")
    f = tmp_path / "a.fused"
    f.write_bytes(raw_container({"fused_app_file": 2, "files": [
        {"path": "big", "offset": 0, **meta, "size": CAPS["max_entry_bytes"] + 1}]}, comp))
    with pytest.raises(c.ContainerError, match="too large"):
        c.read_index(str(f), **CAPS)
    f.write_bytes(raw_container({"fused_app_file": 2, "files": [
        {"path": "past", "offset": 10_000, **meta}]}, comp))
    with pytest.raises(c.ContainerError, match="past the end"):
        c.read_index(str(f), **CAPS)
    f.write_bytes(raw_container({"fused_app_file": 2, "files": [
        {"path": f"f{i}", "offset": 0, **meta} for i in range(CAPS["max_entries"] + 1)]}, comp))
    with pytest.raises(c.ContainerError, match="too many"):
        c.read_index(str(f), **CAPS)


def test_extract_rejects_body_that_disagrees_with_index(tmp_path):
    # Declared size/hash are attacker-controlled; the body is checked against
    # them as it is written, and a mismatch leaves nothing behind.
    comp, meta = blob(b"hello")
    for tweak in ({"size": 4}, {"size": 6}, {"sha256": "0" * 64}):
        f = tmp_path / "a.fused"
        f.write_bytes(raw_container(
            {"fused_app_file": 2, "files": [{"path": "h.txt", "offset": 0, **meta, **tweak}]}, comp))
        idx = c.read_index(str(f), **CAPS)
        dest = tmp_path / "dest"
        with pytest.raises(c.ContainerError, match="declared size|does not match"):
            c.extract(str(f), idx, str(dest))
        assert not dest.exists()


def test_read_member_is_bounded(tmp_path):
    out = build(tmp_path, [("index.html", b"a" * 1000)])
    idx = c.read_index(str(out), **CAPS)
    got = c.read_member(str(out), idx, "index.html", 10)
    assert len(got) == 11  # one past the cap, never the declared 1000
