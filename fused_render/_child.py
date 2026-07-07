"""Worker-process entry point.

Reads a JSON request {"path": ..., "params": {...}} from stdin, imports the
target module, calls its `main(**params)`, and prints a single JSON result
line to stdout. Runs in its own process so user code cannot take down the
server; the parent enforces the timeout.

User print() output is captured and returned in the result payload so it
cannot corrupt the stdout protocol.
"""
import importlib.util
import inspect
import io
import json
import os
import sys
import traceback


class ParamError(TypeError):
    pass


def coerce(value, annotation):
    """Best-effort coercion of string params using type annotations."""
    if annotation is inspect.Parameter.empty:
        return value
    try:
        if annotation is bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if annotation in (int, float, str) and not isinstance(value, annotation):
            return annotation(value)
    except (TypeError, ValueError) as e:
        raise ParamError(f"could not convert param to {annotation.__name__}: {e}") from e
    return value


def bind_params(fn, params):
    sig = inspect.signature(fn)
    has_var_kwargs = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    kwargs = {}
    for name, p in sig.parameters.items():
        if p.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
            continue
        if name in params:
            kwargs[name] = coerce(params[name], p.annotation)
        elif p.default is inspect.Parameter.empty:
            raise ParamError(f"missing required param: {name!r}")
    if has_var_kwargs:
        for k, v in params.items():
            if k not in kwargs:
                kwargs[k] = v
    return kwargs


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
        out = {
            "ok": False,
            "error": {
                "type": type(e).__name__,
                "message": str(e),
                "traceback": traceback.format_exc(),
            },
        }
    finally:
        sys.stdout = real_stdout
    out["stdout"] = captured.getvalue()
    print(json.dumps(out))


if __name__ == "__main__":
    run()
