"""Param binding shared by the two execution paths.

`main(**params)` is called from two places that must bind arguments
identically: the isolated worker subprocess (`_child.py`, user code) and the
in-process runner for first-party helpers (`executor.py`, D72). Keeping the
coercion in one module means both paths agree on how string params from the URL
map onto annotated signatures.

There is a third consumer that does not import this module: the fused engine's
child cannot see the package at all (the local backend strips PYTHONPATH), so
`engine.build_code` reads **this file's source** and `exec`s it inside the code
it generates (D167). Consequences for anything edited here: keep it stdlib-only
and self-contained (no `fused_render.*` imports, no reliance on module state),
and remember that a change to the coercion rules changes both engines at once —
which is the point. `tests/test_engine_parity.py` holds them to it.
"""
import inspect


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
    # eval_str resolves *string* annotations to the real objects. Without it, a
    # module with `from __future__ import annotations` (PEP 563) — or any
    # hand-quoted annotation — hands us the string "int" instead of `int`, and
    # every coercion rule below silently misses: the URL's "7" reaches main()
    # as the string "7".
    #
    # But eval_str *evaluates* those strings, and an annotation is allowed to be
    # anything: `def main(path: "path to the file")` is a documentation habit,
    # not a type, and evaluating it raises SyntaxError. Any exception here has
    # to degrade to "no coercion for that param" — the same treatment an
    # un-annotated param gets, and what this signature did before eval_str
    # existed. Narrowing this to NameError/TypeError (the failures a
    # TYPE_CHECKING-only import or a typo produce) is what turned a
    # previously-ignored annotation into a hard bind-time failure that killed
    # every call to the file, under both engines.
    try:
        sig = inspect.signature(fn, eval_str=True)
    except Exception:  # noqa: BLE001 — an unevaluatable annotation is not fatal
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
