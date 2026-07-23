"""Param binding + error diagnostics shared by the two execution paths.

`main(**params)` is called from two places that must behave identically: the
isolated worker subprocess (`_child.py`, user code) and the in-process runner
for first-party helpers (`executor.py`, D72). Keeping the shared pieces in one
module means both paths agree on:

  * how string params from the URL map onto annotated signatures
    (`coerce`/`bind_params`);
  * how a failure is reported to the page (`trim_harness_frames`/
    `user_location`, D132) — the traceback starts at the caller's code and the
    error dict carries a structured `where` pointing at the failing line.
"""
import inspect
import os
import traceback


class ParamError(TypeError):
    pass


def trim_harness_frames(tb, harness_files):
    """Drop the leading traceback frames that belong to the runner.

    Every caught traceback starts with the runner's own frame(s) plus the
    frozen importlib frames `exec_module` routes through — constant noise that
    buries the user's file. `harness_files` is the set of absolute paths that
    count as runner internals (the executing module + `_binding.py`); skip
    leading frames whose file is one of those or a `<frozen …>` bootstrap, and
    keep everything from the first non-runner frame down untouched. Returns
    None when nothing remains — the error was raised by the harness itself (bad
    params, missing `main`, unserializable return), where the message alone is
    the whole story.
    """
    while tb is not None:
        filename = tb.tb_frame.f_code.co_filename
        if not filename.startswith("<frozen") and os.path.abspath(filename) not in harness_files:
            break
        tb = tb.tb_next
    return tb


def user_location(exc, path):
    """The deepest traceback frame inside the user's own file, as a dict.

    `path` is the absolute path of the script being run. This is the line to
    blame in the code the user wrote: an error raised inside a library still
    points at the user line that called into it. A SyntaxError never gets a
    frame for the unparsed file, so its location comes off the exception
    itself. Returns None when the error never touched the user's file
    (harness-raised errors).
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
