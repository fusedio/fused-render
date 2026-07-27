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

# The flip side of that same script invocation: sys.path[0] is the PACKAGE
# directory, not its parent, so `import fused_render` does NOT resolve here
# unless the package happens to be pip-installed into this interpreter. A helper
# that delegates to the package therefore died in the child with "No module
# named 'fused_render'" — which is how the call-log reader (it reads the store
# through `fused_render.calls`) failed while log_studio's stdlib-only reader
# worked, making a child-bootstrap bug look like a call-log bug.
#
# Appended rather than inserted: a real installation keeps precedence, and so
# does the user's own module directory that run() puts at sys.path[0].
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PACKAGE_PARENT not in sys.path:
    sys.path.append(_PACKAGE_PARENT)


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
        if isinstance(e, ImportError) and (e.name or "").split(".")[0] == "fused_render":
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
