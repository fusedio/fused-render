"""Read-only-file gates for the slides template (SPEC §13.5, RO-3/RO-4/RO-6).

slides.py is a runPython target (not a package module) that does `import engine`
at module top; engine.py needs python-pptx, which isn't in the test venv. So —
like test_annotate_comments.py — slides.py is loaded via importlib, but with a
stub `engine` module injected into sys.modules first. Every gated path must
raise BEFORE any engine call, so the stub's build/parse functions raise if
touched: the tests prove both the PermissionError and the ordering.

Gates under test:
  * action="save" refuses a chmod -w .pptx up front (before _load_model /
    mkstemp — the atomic tempfile+os.replace would silently bypass the file's
    read-only bit via the parent directory).
  * action="set_title" refuses when its `<file>.json` sidecar isn't writable:
    an EXISTING sidecar needs W_OK on itself, a fresh one W_OK on the parent
    directory (same rule as annotate's _sidecar_writable).
  * _editability(file) is the RO-4 verdict the open action folds into its
    response (editable / readonly_message / readonly_tooltip).
Read-only never blocks viewing or the cache-model autosave — only the explicit
overwrite of the .pptx and the sidecar title write are gated.
"""

import importlib.util
import json
import os
import sys
import types

import pytest

SLIDES_PY = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "slides", "slides.py"
)

# os.access always says yes for root, so the chmod-based gates can't trip.
pytestmark = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="read-only bits are ignored when running as root",
)


def _boom(*args, **kwargs):
    raise AssertionError("engine must not be called — the read-only gate has to fire first")


@pytest.fixture
def slides(tmp_path):
    """Load slides.py with a stub engine module and a tmp cache root."""
    saved = sys.modules.get("engine")
    stub = types.ModuleType("engine")
    stub.ENGINE_V = 0  # referenced by _content_hash at runtime
    stub.build_pptx = _boom
    stub.parse_pptx = _boom
    stub.build_pdf = _boom
    stub.build_html = _boom
    stub.build_md = _boom
    sys.modules["engine"] = stub
    try:
        spec = importlib.util.spec_from_file_location("slides_target", SLIDES_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.CACHE_ROOT = str(tmp_path / "cache")  # keep main() away from ~
        # pre-create: main()'s makedirs must survive tests that chmod tmp_path
        os.makedirs(mod.CACHE_ROOT, exist_ok=True)
        yield mod
    finally:
        if saved is not None:
            sys.modules["engine"] = saved
        else:
            sys.modules.pop("engine", None)


def _deck(tmp_path):
    f = tmp_path / "deck.pptx"
    f.write_bytes(b"not-a-real-pptx")
    return f


# --------------------------------------------------------------- save gate
def test_save_on_readonly_pptx_raises_before_engine(slides, tmp_path):
    f = _deck(tmp_path)
    os.chmod(f, 0o444)
    try:
        # doc "deadbeef" has no cached model on purpose: the guard must run
        # BEFORE _load_model (guard-first), or this raises FileNotFoundError.
        with pytest.raises(PermissionError, match="read-only"):
            slides.main(action="save", file=str(f), doc="deadbeef")
        assert f.read_bytes() == b"not-a-real-pptx"
    finally:
        os.chmod(f, 0o644)


# ------------------------------------------------------------ sidecar gate
def test_set_title_readonly_sidecar_raises_and_preserves_bytes(slides, tmp_path):
    f = _deck(tmp_path)
    sidecar = tmp_path / "deck.pptx.json"
    original = b'{"slides": {"title": "Old"}}'
    sidecar.write_bytes(original)
    os.chmod(sidecar, 0o444)
    try:
        with pytest.raises(PermissionError, match="read-only"):
            slides.main(action="set_title", file=str(f), title="New")
        assert sidecar.read_bytes() == original
    finally:
        os.chmod(sidecar, 0o644)


def test_set_title_readonly_dir_without_sidecar_raises(slides, tmp_path):
    f = _deck(tmp_path)
    os.chmod(tmp_path, 0o555)
    try:
        with pytest.raises(PermissionError, match="read-only"):
            slides.main(action="set_title", file=str(f), title="New")
        assert not (tmp_path / "deck.pptx.json").exists()
    finally:
        os.chmod(tmp_path, 0o755)


def test_set_title_creates_sidecar_in_writable_dir(slides, tmp_path):
    f = _deck(tmp_path)
    res = slides.main(action="set_title", file=str(f), title="My Deck")
    assert res == {"ok": True, "title": "My Deck"}
    data = json.loads((tmp_path / "deck.pptx.json").read_text())
    assert data["slides"]["title"] == "My Deck"


# --------------------------------------------------------- open verdict (RO-4)
def test_editability_verdict(slides, tmp_path):
    f = _deck(tmp_path)
    assert slides._editability(str(f)) == (True, "", "")
    os.chmod(f, 0o444)
    try:
        editable, message, tooltip = slides._editability(str(f))
        assert editable is False
        assert message == "Read-only"
        assert "read-only" in tooltip
    finally:
        os.chmod(f, 0o644)


def test_sidecar_writable_helper(slides, tmp_path):
    f = _deck(tmp_path)
    assert slides._sidecar_writable(str(f)) is True  # fresh sidecar, writable dir
    sidecar = tmp_path / "deck.pptx.json"
    sidecar.write_text("{}")
    os.chmod(sidecar, 0o444)
    try:
        assert slides._sidecar_writable(str(f)) is False
    finally:
        os.chmod(sidecar, 0o644)
