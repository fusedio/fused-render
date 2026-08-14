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


def _fake_pypandoc(monkeypatch, convert):
    import types
    mod = types.ModuleType("pypandoc")
    mod.convert_file = convert
    monkeypatch.setitem(sys.modules, "pypandoc", mod)   # export does `import pypandoc`


def test_export_calls_pypandoc_and_returns_the_file(eng, tmp_path, monkeypatch):
    monkeypatch.setattr(eng, "_export_dir_for", lambda p: str(tmp_path))
    seen = {}

    def convert(src, to, format=None, outputfile=None, extra_args=None):
        seen.update(src=src, to=to, format=format)
        open(outputfile, "w", encoding="utf-8").write("converted")
    _fake_pypandoc(monkeypatch, convert)

    r = eng.main(action="export", path=_tex(tmp_path), target="md")
    assert r.get("name", "").endswith(".md") and r.get("size", 0) > 0 and "error" not in r
    assert seen["to"] == "gfm" and seen["format"] == "latex+raw_tex"


def test_export_surfaces_a_pypandoc_failure(eng, tmp_path, monkeypatch):
    monkeypatch.setattr(eng, "_export_dir_for", lambda p: str(tmp_path))

    def convert(*a, **k):
        raise RuntimeError("pandoc boom")
    _fake_pypandoc(monkeypatch, convert)

    r = eng.main(action="export", path=_tex(tmp_path), target="html")
    assert "error" in r and "pandoc boom" in r["error"]


def test_pandoc_venv_returns_cached_without_building(eng, tmp_path, monkeypatch):
    pandoc = tmp_path / "_pandoc"
    vdir = pandoc / "venv" / ("Scripts" if os.name == "nt" else "bin")
    vdir.mkdir(parents=True)
    vpy = vdir / ("python.exe" if os.name == "nt" else "python")
    vpy.write_text("", encoding="utf-8")
    (pandoc / "deps_ok").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(eng, "PANDOC_DIR", str(pandoc))
    monkeypatch.setattr(eng.subprocess, "run",
                        lambda *a, **k: pytest.fail("must not rebuild an existing venv"))
    assert eng._pandoc_venv_python() == str(vpy)


def test_pandoc_venv_builds_under_lock_and_releases_it(eng, tmp_path, monkeypatch):
    pandoc = tmp_path / "_pandoc"
    monkeypatch.setattr(eng, "PANDOC_DIR", str(pandoc))
    monkeypatch.setattr(eng.shutil, "which", lambda n: "uv" if n == "uv" else None)
    runs = []

    def fake_run(cmd, **kw):
        runs.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(eng.subprocess, "run", fake_run)

    eng._pandoc_venv_python()
    assert any("venv" in c for c in runs) and any("install" in c for c in runs)
    assert os.path.exists(os.path.join(str(pandoc), "deps_ok"))
    assert not os.path.exists(os.path.join(str(pandoc), "build.lock"))   # released in finally


def test_export_falls_back_to_ondemand_venv_when_pypandoc_missing(eng, tmp_path, monkeypatch):
    # The built-in engine runs export on the app interpreter (no folder venv), so
    # `import pypandoc` fails and we must fetch pandoc on demand instead of crashing.
    import json as _json
    monkeypatch.setattr(eng, "_export_dir_for", lambda p: str(tmp_path))
    monkeypatch.setitem(sys.modules, "pypandoc", None)          # import pypandoc -> ImportError
    monkeypatch.setattr(eng, "_pandoc_venv_python", lambda: "pyexe")

    def fake_run(cmd, **kw):
        a = _json.loads(cmd[-1])                                 # the conversion job rides in argv
        open(a["out"], "w", encoding="utf-8").write("converted")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(eng.subprocess, "run", fake_run)

    r = eng.main(action="export", path=_tex(tmp_path), target="md")
    assert r.get("name", "").endswith(".md") and r.get("size", 0) > 0 and "error" not in r
