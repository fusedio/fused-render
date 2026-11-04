"""Line endings on the canvas body channel.

The workbench editor holds LF text. Before this was pinned, the clone wrote
bodies in text mode, so Windows put CRLF on disk; disk then never read back as
the text the frame sent, every delta carried the file, the frame normalised it
to LF and posted it again — an endless `body written` / `delta cut` loop a few
times a second. These tests pin both halves: bodies are written verbatim, and
a CRLF file (a Windows editor, git `autocrlf`) reads as the LF text the frame
holds.
"""

from __future__ import annotations

from fused_render import canvas_snapshot
from test_canvas_sync_watcher import _quiet_watcher, _start
from test_canvases import (  # noqa: F401 — fixtures are used by name
    _cloned_shim_harness,
    _reset_module_state,
    fake_agent,
    harness,
)

LF_TEXT = "import fused\n\n@fused.udf\ndef udf():\n    return 1\n"
CRLF_TEXT = LF_TEXT.replace("\n", "\r\n")


def test_write_atomic_writes_text_byte_for_byte(tmp_path):
    path = tmp_path / "x.py"
    canvas_snapshot._write_atomic(str(path), LF_TEXT)
    assert path.read_bytes() == LF_TEXT.encode("utf-8")  # no "\r\n" on Windows
    canvas_snapshot._write_atomic(str(path), CRLF_TEXT)
    assert path.read_bytes() == CRLF_TEXT.encode("utf-8")  # no "\r\r\n" either


def test_a_written_body_reads_back_as_the_same_text(tmp_path):
    path = tmp_path / "x.py"
    canvas_snapshot._write_atomic(str(path), LF_TEXT)
    assert canvas_snapshot.read_body(str(path)) == LF_TEXT


def test_a_crlf_file_reads_as_the_lf_text_the_frame_holds(tmp_path):
    path = tmp_path / "x.py"
    path.write_bytes(CRLF_TEXT.encode("utf-8"))
    assert canvas_snapshot.read_body(str(path)) == LF_TEXT
    assert canvas_snapshot.body_hash(canvas_snapshot.read_body(str(path))) == \
        canvas_snapshot.body_hash(LF_TEXT)


def test_normalize_newlines_handles_lone_cr():
    assert canvas_snapshot.normalize_newlines("a\rb\r\nc\n") == "a\nb\nc\n"


def test_apply_body_does_not_rewrite_a_crlf_file_with_the_same_text(
        harness, tmp_path, monkeypatch, fake_agent):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    path = harness.root / "alpha" / "a.py"
    path.write_bytes(CRLF_TEXT.encode("utf-8"))
    with m._op_lock:
        ack = m.apply_body("a", "a.py", canvas_snapshot.body_hash(LF_TEXT), LF_TEXT)
    assert ack["ok"] is True and ack["changed"] is False, ack
    assert path.read_bytes() == CRLF_TEXT.encode("utf-8")


def test_apply_body_writes_lf_and_a_repost_is_a_no_op(
        harness, tmp_path, monkeypatch, fake_agent):
    """The loop itself: the frame posts a body, then posts the same text again
    (as it does after the delta that follows). The second post must find disk
    equal and write nothing."""
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    path = harness.root / "alpha" / "a.py"
    path.write_bytes(b"a1\r\n")
    with m._op_lock:
        first = m.apply_body("a", "a.py", canvas_snapshot.body_hash("a1\n"), LF_TEXT)
    assert first["ok"] is True and first["changed"] is True, first
    assert path.read_bytes() == LF_TEXT.encode("utf-8")
    with m._op_lock:
        again = m.apply_body("a", "a.py", first["hash"], LF_TEXT)
    assert again["ok"] is True and again["changed"] is False, again
