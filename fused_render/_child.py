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


def _trim_harness_frames(tb):
    """Drop the leading traceback frames that belong to this runner.

    Every user-code traceback starts with run()'s own frame plus the frozen
    importlib frames exec_module routes through — constant noise that buries
    the user's file. Skip leading frames whose file is this module or a
    `<frozen …>` bootstrap; everything from the first user/library frame down
    is kept untouched. Returns None when nothing remains — the error was
    raised by the harness itself (bad params, missing main, unserializable
    return), where the message alone is the whole story.
    """
    here = os.path.abspath(__file__)
    while tb is not None:
        filename = tb.tb_frame.f_code.co_filename
        if not filename.startswith("<frozen") and os.path.abspath(filename) != here:
            break
        tb = tb.tb_next
    return tb


def _user_location(exc, path):
    """The deepest traceback frame inside the user's own file, as a dict.

    This is the line to blame in the script the user wrote: an error raised
    inside a library still points at the user line that called into it. A
    SyntaxError never gets a frame for the unparsed file, so its location
    comes off the exception itself. None when the error never touched the
    user's file (harness-raised errors).
    """
    if isinstance(exc, SyntaxError) and exc.filename and os.path.abspath(exc.filename) == path:
        return {
            "file": path,
            "line": exc.lineno,
            "func": None,
            "source": (exc.text or "").strip() or None,
        }
    location = None
    for frame in traceback.extract_tb(exc.__traceback__):
        if os.path.abspath(frame.filename) == path:
            location = {
                "file": path,
                "line": frame.lineno,
                "func": frame.name,
                "source": frame.line or None,
            }
    return location


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
        tb = _trim_harness_frames(e.__traceback__)
        if tb is None:
            # Harness-raised (bad params, missing main, unserializable
            # return): the message is the whole story — no stack, and no
            # chained-cause frames (those would be runner internals too).
            formatted = "".join(traceback.format_exception_only(type(e), e))
        else:
            formatted = "".join(traceback.format_exception(type(e), e, tb))
        out = {
            "ok": False,
            "error": {
                "type": type(e).__name__,
                "message": str(e),
                "traceback": formatted,
                "where": _user_location(e, path),
            },
        }
    finally:
        sys.stdout = real_stdout
    out["stdout"] = captured.getvalue()
    print(json.dumps(out))


if __name__ == "__main__":
    run()
