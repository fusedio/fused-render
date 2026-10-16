"""Backend tests for the latex template's compile / cache-warm behaviour.

engine.py is a runPython target (not a package module); it does `import
procutil` from templates/shared at module top. Like test_slides_readonly, we
load it via importlib with that dir on sys.path. Tectonic is never invoked for
real — `subprocess.run` and the bin/cache helpers are stubbed — so these stay
fast and hermetic.

Lives with the latex template. Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/latex/tests/test_latex_compile.py -o addopts=""
"""
import importlib.util
import os
import subprocess
import sys

import pytest

_LATEX = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHARED = os.path.join(os.path.dirname(_LATEX), "shared")


def _load_engine():
    if _SHARED not in sys.path:
        sys.path.insert(0, _SHARED)
    spec = importlib.util.spec_from_file_location(
        "latex_engine", os.path.join(_LATEX, "engine.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def eng(tmp_path, monkeypatch):
    e = _load_engine()
    monkeypatch.setattr(e, "_tectonic_bin", lambda: "tectonic")   # pretend it's installed
    build = tmp_path / "build"
    build.mkdir()
    monkeypatch.setattr(e, "_build_dir_for", lambda p: str(build))
    e._build = str(build)  # stash for tests
    return e


def _tex(tmp_path):
    main = tmp_path / "main.tex"
    main.write_text(r"\documentclass{article}\begin{document}x\end{document}",
                    encoding="utf-8")
    return str(main)


def test_compile_defers_to_warming_when_cache_cold(eng, tmp_path, monkeypatch):
    monkeypatch.setattr(eng, "_cache_warm", lambda: False)
    warmed = []
    monkeypatch.setattr(eng, "_ensure_warming", lambda mp: warmed.append(mp))
    monkeypatch.setattr(eng, "_warm_progress", lambda: {"stage": "spawn"})
    main = _tex(tmp_path)
    r = eng.main(action="compile", path=main)
    assert r["warming"] is True and r["ok"] is False
    assert warmed == [main]                       # warms THIS document, not a scaffold


def test_compile_surfaces_unexplained_failure(eng, tmp_path, monkeypatch):
    # warm cache, but tectonic crashes: non-zero exit, a stdout note, empty
    # stderr, and no .pdf / .log written — the pre-fix "blank ? errors" case.
    monkeypatch.setattr(eng, "_cache_warm", lambda: True)

    def fake_run(cmd, env, cwd, **kw):
        return False, subprocess.CompletedProcess(cmd, 127, stdout="note: Running TeX ...\n", stderr="")
    monkeypatch.setattr(eng, "_run_tectonic", fake_run)

    r = eng.main(action="compile", path=_tex(tmp_path), force=1)
    assert r["ok"] is False and not r.get("warming")
    assert r.get("error"), "a failed compile must surface a message, not a blank ? errors"
    assert "127" in r["error"]                    # the exit code
    assert "Running TeX" in r["error"]            # the stdout note (tail now includes stdout)


def test_compile_ok_when_pdf_is_produced(eng, tmp_path, monkeypatch):
    monkeypatch.setattr(eng, "_cache_warm", lambda: True)

    def fake_run(cmd, env, cwd, **kw):
        open(os.path.join(eng._build, "main.pdf"), "wb").close()   # tectonic wrote the PDF
        return False, subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(eng, "_run_tectonic", fake_run)

    r = eng.main(action="compile", path=_tex(tmp_path), force=1)
    assert r["ok"] is True and r["pdf"] and not r.get("error")


def test_compile_defers_when_run_tectonic_bails_on_cold_fetch(eng, tmp_path, monkeypatch):
    # Warm cache marker present, but THIS document needs an uncached package, so
    # _run_tectonic bails to the background warmer instead of blocking the budget.
    monkeypatch.setattr(eng, "_cache_warm", lambda: True)
    warmed = []
    monkeypatch.setattr(eng, "_ensure_warming", lambda mp: warmed.append(mp))
    monkeypatch.setattr(eng, "_warm_progress", lambda: {"stage": "warm"})
    monkeypatch.setattr(eng, "_run_tectonic", lambda cmd, env, cwd, **kw: (True, None))

    main = _tex(tmp_path)
    r = eng.main(action="compile", path=main, force=1)
    assert r["warming"] is True and r["ok"] is False
    assert warmed == [main]


def test_should_defer_download_only_on_a_sustained_cold_fetch(eng):
    d = eng._should_defer_download
    many = "note: downloading " * 8
    # A pure typesetting run (a warm cache) downloads nothing — never defer.
    assert d("", 30.0) is False
    # A brief burst inside the grace window is not yet a verdict.
    assert d(many, 0.5) is False
    # A quick self-heal of a couple of packages runs inline to completion.
    assert d("note: downloading a\nnote: downloading b\n", 5.0) is False
    # Many files still arriving past the grace window: this is the big cold fetch.
    assert d(many, 5.0) is True


def _fake_pandoc(eng, tmp_path, monkeypatch, returncode=0):
    monkeypatch.setattr(eng, "_export_dir_for", lambda p: str(tmp_path))
    monkeypatch.setattr(eng.pandoc_bin, "path", lambda: "pandoc")
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        if returncode == 0:
            out = next(a for a in cmd if a.startswith("--output="))[len("--output="):]
            open(out, "w", encoding="utf-8").write("converted")
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="pandoc boom")
    monkeypatch.setattr(eng.subprocess, "run", fake_run)
    return seen


def test_export_runs_pandoc_and_returns_the_file(eng, tmp_path, monkeypatch):
    seen = _fake_pandoc(eng, tmp_path, monkeypatch)
    r = eng.main(action="export", path=_tex(tmp_path), target="md")
    assert r.get("name", "").endswith(".md") and r.get("size", 0) > 0 and "error" not in r
    assert seen[0][:3] == ["pandoc", "--from=latex+raw_tex", "--to=gfm"]


def test_export_surfaces_a_pandoc_failure_and_a_missing_pandoc(eng, tmp_path, monkeypatch):
    _fake_pandoc(eng, tmp_path, monkeypatch, returncode=1)
    r = eng.main(action="export", path=_tex(tmp_path), target="html")
    assert "pandoc boom" in r["error"] and not r.get("missing_pandoc")

    monkeypatch.setattr(eng.pandoc_bin, "path", lambda: None)
    r = eng.main(action="export", path=_tex(tmp_path), target="html")
    assert r["missing_pandoc"] is True
