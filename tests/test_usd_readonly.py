"""Read-only sidecar gating for the usd template (SPEC §13.5, RO-6).

reader.py is a stdlib-at-import runPython target (numpy/msgpack/usd-core are
convert-time deps, not import-time), so — like test_annotate_comments.py —
these load it via importlib and drive `_sidecar_writable`/`main` directly.

The usd template never writes the viewed asset; its only write target is the
settings sidecar `<file>.json` saved from JS via fused.writeFile. The reader's
`inspect` action reports `sidecar_writable` so the template can stop firing
doomed saves and show the shared ro-badge. Writability rule: existing sidecar
→ W_OK on itself; absent → W_OK on the parent dir (the JS write lands there).

Tests use a `.splat` target: inspect's direct branch needs only os.path, no
cache dir and no heavy deps. CACHE_ROOT is repointed at tmp_path anyway so
nothing can touch the real home.
"""

import importlib.util
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
READER_PY = os.path.join(HERE, os.pardir, "fused_render", "templates", "usd", "reader.py")

# os.access always says yes for root, so the chmod-based gates can't trip.
skip_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="read-only bits are ignored when running as root")


def _load_reader(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("usd_reader_target", READER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "CACHE_ROOT", str(tmp_path / "cache"))
    return mod


def _asset(tmp_path):
    f = tmp_path / "scene.splat"
    f.write_bytes(b"\x00" * 32)
    return f


# ---------------------------------------------------------- _sidecar_writable


def test_sidecar_writable_no_sidecar_writable_dir(tmp_path, monkeypatch):
    mod = _load_reader(tmp_path, monkeypatch)
    f = _asset(tmp_path)
    assert mod._sidecar_writable(str(f)) is True


@skip_root
def test_sidecar_writable_existing_readonly_sidecar(tmp_path, monkeypatch):
    mod = _load_reader(tmp_path, monkeypatch)
    f = _asset(tmp_path)
    sidecar = tmp_path / "scene.splat.json"
    sidecar.write_text("{}")
    os.chmod(sidecar, 0o444)
    try:
        assert mod._sidecar_writable(str(f)) is False
    finally:
        os.chmod(sidecar, 0o644)


@skip_root
def test_sidecar_writable_readonly_parent_no_sidecar(tmp_path, monkeypatch):
    mod = _load_reader(tmp_path, monkeypatch)
    d = tmp_path / "locked"
    d.mkdir()
    f = d / "scene.splat"
    f.write_bytes(b"\x00" * 32)
    os.chmod(d, 0o555)
    try:
        assert mod._sidecar_writable(str(f)) is False
    finally:
        os.chmod(d, 0o755)


# ------------------------------------------------------------ inspect action


def test_inspect_reports_sidecar_writable_true(tmp_path, monkeypatch):
    mod = _load_reader(tmp_path, monkeypatch)
    f = _asset(tmp_path)
    out = mod.main(action="inspect", file=str(f))
    assert out.get("kind") == "splat-direct"
    assert out["sidecar_writable"] is True


@skip_root
def test_inspect_reports_sidecar_writable_false(tmp_path, monkeypatch):
    mod = _load_reader(tmp_path, monkeypatch)
    f = _asset(tmp_path)
    sidecar = tmp_path / "scene.splat.json"
    sidecar.write_text("{}")
    os.chmod(sidecar, 0o444)
    try:
        out = mod.main(action="inspect", file=str(f))
        assert out["sidecar_writable"] is False
    finally:
        os.chmod(sidecar, 0o644)
