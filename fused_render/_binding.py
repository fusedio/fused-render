"""Param binding shared by the two execution paths.

`main(**params)` is called from two places that must bind arguments
identically: the isolated worker subprocess (`_child.py`, user code) and the
in-process runner for first-party helpers (`executor.py`, D72). Keeping the
coercion in one module means both paths agree on how string params from the URL
map onto annotated signatures.
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
    sig = inspect.signature(fn)
    has_var_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
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
