"""Tests for first-run onboarding (fused_render/shell/seed.py, D81): the
~/Documents/Fused workspace dir.

FUSED_RENDER_DIR (the Fused dir) is redirected to a tmp dir so no test touches
a real dir.
"""
from fused_render.shell.seed import ensure_fused_dir


def _setup(tmp_path, monkeypatch):
    fdir = tmp_path / "Documents" / "Fused"
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return fdir


def test_creates_missing_dir(tmp_path, monkeypatch):
    fdir = _setup(tmp_path, monkeypatch)
    returned = ensure_fused_dir()

    assert returned == str(fdir)
    assert fdir.is_dir()


def test_existing_content_is_left_untouched(tmp_path, monkeypatch):
    fdir = _setup(tmp_path, monkeypatch)
    fdir.mkdir(parents=True)
    (fdir / "my_work.html").write_text("mine", encoding="utf-8")

    ensure_fused_dir()

    assert (fdir / "my_work.html").read_text(encoding="utf-8") == "mine"


def test_idempotent_second_run_is_noop(tmp_path, monkeypatch):
    fdir = _setup(tmp_path, monkeypatch)
    ensure_fused_dir()
    ensure_fused_dir()

    assert fdir.is_dir()
