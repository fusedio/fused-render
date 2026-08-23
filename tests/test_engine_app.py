"""Warm-worker app engine (/api/engine): interpreter choice, engine id,
validation, and the worker's warm-state / mtime-reload / error envelope."""
import os
import sys

import pytest

import fused_render
from fused_render import projectenv
from fused_render.server import engine_host


def _load_engine_worker():
    # engine_worker.py is spawned as a script (its dir on sys.path[0]) so it can
    # `from _binding import bind_params`; import it the same way to test _Target.
    pkg_dir = os.path.dirname(fused_render.__file__)
    if pkg_dir not in sys.path:
        sys.path.insert(0, pkg_dir)
    import engine_worker

    return engine_worker


def test_interpreter_for_no_env_is_app_interpreter():
    assert projectenv.interpreter_for(None) == sys.executable


def test_app_engine_id_is_stable_bare_and_per_path():
    a = engine_host.app_engine_id("/tmp/proj/app.py")
    assert a == engine_host.app_engine_id("/tmp/proj/app.py")
    assert a != engine_host.app_engine_id("/tmp/proj/other.py")
    assert a.startswith("app_")
    assert engine_host._ENGINE_ID.match(a)


def test_ensure_app_rejects_foreign_interpreter(tmp_path):
    app = tmp_path / "app.py"
    app.write_text("def main():\n    return {}\n", encoding="utf-8")
    with pytest.raises(engine_host.EngineError):
        engine_host.ensure_app(str(app), "/definitely/not/a/real/python")


def test_warm_target_persists_then_reloads_on_mtime(tmp_path):
    ew = _load_engine_worker()
    mod = tmp_path / "mod.py"
    v1 = ("_n = 0\nMARK = 'v1'\n"
          "def main():\n    global _n\n    _n += 1\n    return {'n': _n, 'mark': MARK}\n")
    mod.write_text(v1, encoding="utf-8")
    target = ew._Target(str(mod))

    assert target.call({})["result"] == {"n": 1, "mark": "v1"}
    assert target.call({})["result"] == {"n": 2, "mark": "v1"}  # warm: globals persist

    mod.write_text(v1.replace("'v1'", "'v2'"), encoding="utf-8")
    st = mod.stat()
    os.utime(str(mod), (st.st_atime + 5, st.st_mtime + 5))  # force a distinct mtime
    assert target.call({})["result"] == {"n": 1, "mark": "v2"}  # re-imported: reset + new code


def test_warm_target_error_envelope(tmp_path):
    ew = _load_engine_worker()
    mod = tmp_path / "bad.py"
    mod.write_text("def main():\n    raise ValueError('boom')\n", encoding="utf-8")
    out = ew._Target(str(mod)).call({})
    assert out["ok"] is False
    assert out["error"]["type"] == "ValueError"
    assert "boom" in out["error"]["message"]
    assert "Traceback" in out["error"]["traceback"]


def test_warm_target_rejects_non_json_result(tmp_path):
    ew = _load_engine_worker()
    mod = tmp_path / "nj.py"
    mod.write_text("def main():\n    return object()\n", encoding="utf-8")
    out = ew._Target(str(mod)).call({})
    assert out["ok"] is False
    assert out["error"]["type"] == "TypeError"
