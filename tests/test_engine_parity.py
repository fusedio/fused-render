"""Cross-engine param-binding parity (SPEC PY-4/PY-13/PY-14).

`main(**params)` is bound by two independent code paths: the built-in
executor's worker imports `fused_render._binding`, while the fused engine's
child *cannot* (the local compute backend strips PYTHONPATH, so
`import fused_render` fails there) and therefore gets the binding logic
embedded into the generated wrapper by `engine.build_code`.

Two implementations of one contract is exactly the shape that drifts silently:
the divergence this file was written for was `from __future__ import
annotations` — the fused engine coerced string annotations, the built-in one
passed the raw URL strings straight through, so the same template returned
`"7"` under one engine and `7` under the other. Nothing failed; the numbers
were just wrong.

So the table below is run through both engines and compared, on both halves of
the wire shape (PY-14): the bound values AND the surfaced `error.type` — the
latter because runtime.js and the templates branch on `error.type` and must
never be able to tell which engine ran the code.
"""
import asyncio
import json
import os
import traceback

import pytest

from fused_render import engine, executor

# (id, source, params, expected) where expected is either {"result": ...} for a
# successful bind or {"type": "..."} for the error.type both engines must
# surface. Kept deliberately boring: every row is a coercion rule from PY-4.
CASES = [
    ("int-from-string", "def main(n: int = 1):\n    return [n, type(n).__name__]\n", {"n": "160"}, {"result": [160, "int"]}),
    ("float-from-string", "def main(f: float = 0.0):\n    return [f, type(f).__name__]\n", {"f": "2.5"}, {"result": [2.5, "float"]}),
    ("bool-false-word", "def main(b: bool = True):\n    return b\n", {"b": "false"}, {"result": False}),
    ("bool-zero", "def main(b: bool = True):\n    return b\n", {"b": "0"}, {"result": False}),
    ("bool-yes", "def main(b: bool = False):\n    return b\n", {"b": "yes"}, {"result": True}),
    ("bool-already-bool", "def main(b: bool = False):\n    return b\n", {"b": True}, {"result": True}),
    # Unannotated params are passed through verbatim — templates rely on this
    # (templates/structure/reader.py leaves row_group unannotated on purpose).
    ("unannotated-passthrough", "def main(x=None):\n    return [x, type(x).__name__]\n", {"x": "7"}, {"result": ["7", "str"]}),
    # **kwargs receives everything not matched by a named param, uncoerced.
    ("var-kwargs-passthrough", "def main(a: int = 0, **kw):\n    return [a, kw]\n", {"a": "3", "extra": "9"}, {"result": [3, {"extra": "9"}]}),
    # The regression this file exists for: PEP 563 turns every annotation into
    # a string, so a binder that reads __annotations__ literally sees "int".
    (
        "future-annotations",
        "from __future__ import annotations\ndef main(n: int = 1):\n    return [n, type(n).__name__]\n",
        {"n": "7"},
        {"result": [7, "int"]},
    ),
    # Same thing without the __future__ import: a hand-quoted annotation.
    ("quoted-annotation", 'def main(n: "int" = 1):\n    return [n, type(n).__name__]\n', {"n": "5"}, {"result": [5, "int"]}),
    # An annotation naming something that does not exist at bind time must not
    # be fatal — it falls back to "no coercion", not a NameError.
    (
        "unresolvable-annotation",
        "from __future__ import annotations\ndef main(x: Nope = None):\n    return [x, type(x).__name__]\n",
        {"x": "7"},
        {"result": ["7", "str"]},
    ),
    ("missing-required", "def main(x: int):\n    return x\n", {}, {"type": "ParamError"}),
    ("uncoercible-int", "def main(n: int = 1):\n    return n\n", {"n": "abc"}, {"type": "ParamError"}),
    ("uncoercible-float", "def main(f: float = 0.0):\n    return f\n", {"f": "not-a-number"}, {"type": "ParamError"}),
]

IDS = [c[0] for c in CASES]

requires_fused = pytest.mark.skipif(
    not engine.available(), reason="fused package not installed (engine falls back)"
)


def _wire(out: dict) -> dict:
    """Reduce a /api/run response to just what parity is asserted on."""
    if out.get("ok"):
        return {"result": out.get("result")}
    return {"type": out["error"]["type"]}


def _builtin(tmp_path, src: str, params: dict) -> dict:
    """The built-in engine, end to end (real _child.py subprocess)."""
    target = tmp_path / "builtin" / "target.py"
    target.parent.mkdir(exist_ok=True)
    target.write_text(src)
    return _wire(executor.run_python(str(target), params))


def _fused_wrapper(tmp_path, src: str, params: dict) -> dict:
    """The fused engine's generated wrapper, exec'd the way the backend's
    runner does (cwd = an exec dir holding _params.json), with failures put
    through the same cleaner/splitter `run_python` applies to the backend's
    error text — so the comparison is against the `error.type` that would
    actually reach the browser, not the raw exception class.

    This half needs no `fused` install: build_code's output is plain Python.
    """
    script_dir = tmp_path / "page"
    script_dir.mkdir(exist_ok=True)
    exec_dir = tmp_path / "exec"
    exec_dir.mkdir(exist_ok=True)
    (exec_dir / "_params.json").write_text(json.dumps(params))
    target = script_dir / "target.py"
    target.write_text(src)

    code = engine.build_code(src, str(script_dir), str(target))
    g = {}
    cwd = os.getcwd()
    try:
        os.chdir(exec_dir)
        exec(compile(code, "<lambda_exec>", "exec"), g)
    except BaseException:  # noqa: BLE001 — the backend reports any failure as text
        cleaned = engine._clean_error(traceback.format_exc(), str(target))
        err_type, message = engine._split_error(cleaned)
        return _wire({"ok": False, "error": {"type": err_type, "message": message}})
    finally:
        os.chdir(cwd)
    return _wire({"ok": True, "result": g["result"]})


def _fused_backend(tmp_path, src: str, params: dict, monkeypatch) -> dict:
    """The fused engine end to end, through the real local compute backend.

    DEFAULT_REQUIREMENTS is emptied (as in test_engine.py's integration tests)
    so the venv is bare: fast, offline-safe, and irrelevant to binding.
    """
    monkeypatch.setattr(engine, "DEFAULT_REQUIREMENTS", [])
    monkeypatch.setattr(engine, "_backend", None)
    target = tmp_path / "backend" / "target.py"
    target.parent.mkdir(exist_ok=True)
    target.write_text(src)
    return _wire(asyncio.run(engine.run_python(str(target), params)))


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_wrapper_binding_matches_builtin(tmp_path, case):
    _id, src, params, expected = case
    builtin = _builtin(tmp_path, src, params)
    assert builtin == expected, f"built-in engine: {builtin}"
    assert _fused_wrapper(tmp_path, src, params) == builtin


@requires_fused
@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_real_backend_binding_matches_builtin(tmp_path, case, monkeypatch, warm_fused_backend_venv):
    _id, src, params, expected = case
    assert _fused_backend(tmp_path, src, params, monkeypatch) == expected
