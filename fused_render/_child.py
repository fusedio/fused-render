"""Worker-process entry point.

Reads a JSON request {"path": ..., "params": {...}} from stdin, imports the
target module, calls its `main(**params)`, and prints a single JSON result
line to stdout. Runs in its own process so user code cannot take down the
server; the parent enforces the timeout.

User print() output is captured and returned in the result payload so it
cannot corrupt the stdout protocol.
"""
import importlib.util
import io
import json
import os
import sys
import traceback

# Top-level (not `fused_render._binding`) import on purpose: this file is
# invoked as a standalone script (`python .../fused_render/_child.py`), so its
# own directory is sys.path[0] and `_binding.py` next to it always resolves —
# even when the package isn't pip-installed (dev-from-source). The import runs
# before run() mutates sys.path, so a user module dir can't shadow it.
from _binding import bind_params

# NOTE: this worker deliberately does NOT put the package's parent on sys.path.
# It used to (appended), so that a helper could `import fused_render` from the
# child even when the package is not pip-installed into this interpreter — the
# flip side of the script invocation above, where sys.path[0] is the PACKAGE
# directory rather than its parent. It existed for one consumer, the call-log
# reader reading the store through `fused_render.calls`; nothing under
# `templates/` imports `fused_render` any more (SPEC PY-15: a template learns
# about its environment from `templates/shared/appenv.py`, env vars only).
#
# It was also never dependable. The fused local execution backend strips
# PYTHONPATH/PYTHONHOME/VIRTUAL_ENV from its children for venv hermeticity, so a
# template that leaned on the package being importable worked under this executor
# and silently took its fallback branch under the other engine. `executor
# ._child_env()`, the parent half of the same fix, went in the same change — the
# two halves must not disagree. The ImportError diagnostic in run() below stays:
# a USER .py that imports `fused_render` now fails here, and that message is what
# tells them which interpreter looked and where.


def run():
    req = json.load(sys.stdin)
    path = os.path.abspath(req["path"])
    params = req.get("params") or {}

    captured = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = captured
    out = {"ok": False}
    try:
        module_dir = os.path.dirname(path)
        os.chdir(module_dir)  # relative data paths in user code resolve next to the .py
        sys.path.insert(0, module_dir)
        spec = importlib.util.spec_from_file_location("__fused_module__", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        fn = getattr(mod, "main", None)
        if not callable(fn):
            raise AttributeError(
                f"{os.path.basename(path)} does not define a callable 'main' function"
            )

        result = fn(**bind_params(fn, params))
        try:
            json.dumps(result)
        except (TypeError, ValueError):
            raise TypeError(
                f"main() returned {type(result).__name__}, which is not JSON-serializable; "
                "return dict/list/str/number/bool/None (e.g. df.to_dict('records'))"
            ) from None
        out = {"ok": True, "result": result}
    except BaseException as e:  # noqa: BLE001 — includes SystemExit from user code
        message = str(e)
        # A helper that cannot see `fused_render` is a worker-bootstrap problem,
        # not a problem with the helper — and the bare "No module named
        # 'fused_render'" that reached the user said nothing about which
        # interpreter looked, or where. Name the environment so the next report
        # is conclusive instead of a guess about how it was installed.
        #
        # EXACT name, not its first segment: when the package itself is missing
        # Python reports `name` as the top-level package even for a submodule
        # import (`import fused_render.calls` -> name='fused_render'), whereas a
        # missing submodule under a package that IS importable reports the full
        # dotted path ('fused_render.calls'). Matching the first segment
        # therefore attached a bootstrap diagnosis — executable, PYTHONPATH,
        # sys.path — to a plain typo in a submodule name, pointing the reader at
        # an environment that is fine.
        if isinstance(e, ImportError) and e.name == "fused_render":
            message += (
                f" [worker could not see the fused_render package: "
                f"executable={sys.executable}, "
                f"PYTHONPATH={os.environ.get('PYTHONPATH') or '(unset)'}, "
                f"sys.path[:3]={sys.path[:3]}]"
            )
        out = {
            "ok": False,
            "error": {
                "type": type(e).__name__,
                "message": message,
                "traceback": traceback.format_exc(),
            },
        }
    finally:
        sys.stdout = real_stdout
    out["stdout"] = captured.getvalue()
    print(json.dumps(out))


if __name__ == "__main__":
    run()
