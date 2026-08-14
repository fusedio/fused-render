"""Read-only sidecar gating for the usd template (SPEC §13.5, RO-6).

reader.py is a stdlib-at-import runPython target (numpy/msgpack/usd-core are
convert-time deps, not import-time), so — like test_annotate_comments.py —
these load it via importlib and drive `_sidecar_writable`/`main` directly.

The usd template never writes the viewed asset; its only write target is the
settings sidecar (home_dir()/sidecar/<mapped path>.json, D83-reversal) saved
from JS via fused.writeFile. The reader's `inspect` action reports
`sidecar_writable` so the template can stop firing doomed saves and show the
shared ro-badge. Writability rule: existing sidecar → W_OK on itself; absent
→ W_OK on its nearest existing ancestor dir (the JS write lands there once
created).

Tests use a `.splat` target: inspect's direct branch needs only os.path, no
cache dir and no heavy deps. CACHE_ROOT is repointed at tmp_path anyway so
nothing can touch the real home, and FUSED_RENDER_HOME is pinned to an
isolated tmp dir (via _load_reader) so sidecar_path never touches the
developer's real ~/.fused-render.
"""
import importlib.util
import os
from pathlib import Path

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
READER_PY = os.path.join(HERE, os.pardir, "fused_render", "templates",
                         "usd", "reader.py")

# os.access always says yes for root, so the chmod-based gates can't trip.
skip_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="read-only bits are ignored when running as root")


def _load_reader(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    spec = importlib.util.spec_from_file_location("usd_reader_target", READER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "CACHE_ROOT", str(tmp_path / "cache"))
    return mod


def _asset(tmp_path):
    f = tmp_path / "scene.splat"
    f.write_bytes(b"\x00" * 32)
    return f


def _sidecar(mod, f) -> Path:
    # reader.py has no module-level _sidecar_path (only _sidecar_writable,
    # which imports appenv locally) — but loading it already put shared/ on
    # sys.path, so appenv is importable here too.
    import appenv
    return Path(appenv.sidecar_path(str(f)))


# ---------------------------------------------------------- _sidecar_writable

def test_sidecar_writable_no_sidecar_writable_dir(tmp_path, monkeypatch):
    mod = _load_reader(tmp_path, monkeypatch)
    f = _asset(tmp_path)
    assert mod._sidecar_writable(str(f)) is True


@skip_root
def test_sidecar_writable_existing_readonly_sidecar(tmp_path, monkeypatch):
    mod = _load_reader(tmp_path, monkeypatch)
    f = _asset(tmp_path)
    sidecar = _sidecar(mod, f)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("{}")
    os.chmod(sidecar, 0o444)
    try:
        assert mod._sidecar_writable(str(f)) is False
    finally:
        os.chmod(sidecar, 0o644)


@skip_root
def test_sidecar_writable_readonly_ancestor_no_sidecar(tmp_path, monkeypatch):
    # The sidecar's home-dir subtree doesn't exist yet, so writability walks
    # up to the nearest existing ancestor (nearest_existing_dir) — tmp_path
    # itself here, since FUSED_RENDER_HOME (tmp_path/home) hasn't been created.
    # (Unlike before D83-reversal, the asset's OWN directory no longer matters
    # at all — the sidecar lives in a completely separate tree now.)
    mod = _load_reader(tmp_path, monkeypatch)
    f = _asset(tmp_path)
    os.chmod(tmp_path, 0o555)
    try:
        assert mod._sidecar_writable(str(f)) is False
    finally:
        os.chmod(tmp_path, 0o755)


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
    sidecar = _sidecar(mod, f)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("{}")
    os.chmod(sidecar, 0o444)
    try:
        out = mod.main(action="inspect", file=str(f))
        assert out["sidecar_writable"] is False
    finally:
        os.chmod(sidecar, 0o644)
