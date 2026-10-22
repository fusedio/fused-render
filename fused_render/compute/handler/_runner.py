# Derived from openfused (MIT, Copyright (c) 2026 Fused Labs, Inc.); see ../NOTICE.
import base64
import contextlib
import io
import json
import os
import sys
import traceback


def _is_tabular(obj):
    """True for pandas DataFrame/GeoDataFrame or pyarrow Table/RecordBatch.

    Detected by type, without importing pandas/pyarrow (they may be absent when
    the result is not tabular), so plain values never trigger an import.
    """
    root = (type(obj).__module__ or "").split(".")[0]
    name = type(obj).__name__
    if root in ("pandas", "geopandas") and name in ("DataFrame", "GeoDataFrame"):
        return True
    return root == "pyarrow" and name in ("Table", "RecordBatch")


def _serialize_tabular(obj):
    """Serialize a tabular result to Parquet bytes and the records inline value.

    Returns (parquet_bytes, records_json). The inline value is derived by reading
    the just-written Parquet back (`read_parquet(...).to_dict("records")`) so a
    cold run and a later cache hit — which reconstruct from the same bytes — agree
    even when the Parquet round-trip normalizes a dtype/index. A GeoDataFrame is
    written as geoparquet (geometry preserved). `default=str` keeps non-JSON
    scalars (timestamps, etc.) deterministic across cold and hit.
    """
    import pandas as pd

    name = type(obj).__name__
    buf = io.BytesIO()
    if name in ("Table", "RecordBatch"):
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = obj if name == "Table" else pa.Table.from_batches([obj])
        pq.write_table(table, buf)
    else:
        # pandas DataFrame and geopandas GeoDataFrame both expose to_parquet
        # (GeoDataFrame writes geoparquet).
        obj.to_parquet(buf)
    body = buf.getvalue()
    records = pd.read_parquet(io.BytesIO(body)).to_dict(orient="records")
    return body, json.dumps(records, default=str)


def _coerce_param(value, annotation):
    """Annotation-driven coercion for string-typed transport params.

    Serve GET query params and other string channels deliver every value as a
    str; when the entrypoint annotates a param as int/float/bool the runner
    converts it, so the same function works from JSON-typed and string-typed
    callers alike (spec/runtime/runtime-lambda.md § Parameter binding). Values
    already of the annotated type, and unannotated params, pass through as-is.
    """
    import inspect

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
    except (TypeError, ValueError) as _exc:
        raise TypeError("could not convert param to " + annotation.__name__ + ": " + str(_exc))
    return value


def _bind_params(fn, params):
    """Bind `_params.json` values to `fn`'s signature: filter, coerce, validate.

    Params are matched to the signature by name (extras are dropped unless the
    function takes **kwargs), coerced per annotation, and a missing required
    param raises a clean TypeError instead of an opaque call failure. eval_str
    resolves stringized annotations (PEP 563 / `from __future__ import
    annotations`) so coercion still fires; falls back to the raw signature when
    a name cannot be resolved.
    """
    import inspect

    try:
        sig = inspect.signature(fn, eval_str=True)
    except (NameError, TypeError):
        sig = inspect.signature(fn)
    has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    kwargs = {}
    for name, p in sig.parameters.items():
        if p.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
            continue
        if name in params:
            kwargs[name] = _coerce_param(params[name], p.annotation)
        elif p.default is inspect.Parameter.empty:
            raise TypeError("missing required param: " + repr(name))
    if has_var_kw:
        for k, v in params.items():
            if k not in kwargs:
                kwargs[k] = v
    return kwargs


def _unwrap_udf(obj):
    """The inner function of a @fused.udf wrapper, or `obj` unchanged.

    An explicit-entrypoint lookup can land on the `_UDF` wrapper the decorator
    bound to the name; binding must introspect the *inner* function's signature
    (the wrapper's is `(*args, **kwargs)`), so unwrap before binding/calling.
    Detected by the `__openfused_udf__` marker plus `_fn` — never by value.
    """
    if getattr(obj, "__openfused_udf__", False) and getattr(obj, "_fn", None) is not None:
        return obj._fn
    return obj


def _load_params():
    """Read `_params.json` from the working directory (the single parameter
    channel — serve request params, execute_code params, and fan-out
    invoke(**kwargs) all land here; sdk-openfused.md). Absent or null → {}."""
    params_file = os.path.join(os.getcwd(), "_params.json")
    if os.path.exists(params_file):
        with open(params_file) as f:
            return json.load(f) or {}
    return {}


def main():
    code_path = sys.argv[1]
    out_path = sys.argv[2]
    # Optional explicit entrypoint name (argv[3], empty = unset): resolution
    # rule 1 of spec/runtime/runtime-lambda.md § Result logic. When set it is a
    # complete answer — the convention rules (result / @fused.udf) never run.
    _entrypoint_name = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None
    with open(code_path, encoding="utf-8") as _f:
        _code = _f.read()
    _local_vars = {}
    _stdout_buf = io.StringIO()
    _stderr_buf = io.StringIO()
    _error = None
    _warnings = []
    try:
        with contextlib.redirect_stdout(_stdout_buf), contextlib.redirect_stderr(_stderr_buf):
            exec(compile(_code, "<lambda_exec>", "exec"), _local_vars)
        _return_value = _local_vars.get("result")
    except Exception:
        _error = traceback.format_exc()
        _stderr_buf.write(_error)
        _return_value = None
    if _error is None and _entrypoint_name is not None:
        _target = _unwrap_udf(_local_vars.get(_entrypoint_name))
        if not callable(_target):
            _error = (
                "openfused: entrypoint " + repr(_entrypoint_name) + " not found: the "
                "code does not define a callable with that name"
            )
            _stderr_buf.write(_error)
            _return_value = None
        else:
            try:
                _bound = _bind_params(_target, _load_params())
                with (
                    contextlib.redirect_stdout(_stdout_buf),
                    contextlib.redirect_stderr(_stderr_buf),
                ):
                    _return_value = _target(**_bound)
            except Exception:
                _error = traceback.format_exc()
                _stderr_buf.write(_error)
                _return_value = None
    elif _error is None:
        _fused_mod = _local_vars.get("fused") or sys.modules.get("fused")
        _udfs = (
            getattr(_fused_mod, "_registered_udfs", None) if _fused_mod is not None else None
        ) or []
        _has_result = "result" in _local_vars
        if _has_result and _udfs:
            # A `result` variable and a registered @fused.udf are mutually exclusive
            # return contracts (spec/runtime/runtime-lambda.md): a script must pick exactly
            # one so the runner never has to guess the author's intent. Fail loudly
            # rather than silently preferring `result`.
            _error = (
                "openfused: a script may return via a `result` variable or a "
                "@fused.udf entrypoint, but not both; found both in this code"
            )
            _stderr_buf.write(_error)
            _return_value = None
        elif _udfs:
            # Redefining the SAME name repeatedly (notebook-style iteration) is
            # fine; two DISTINCT decorated functions is ambiguous — the last one
            # wins today, but that tie-break is deprecated (pass `entrypoint` or
            # decorate exactly one function).
            _names = [getattr(_u, "__name__", "<unnamed>") for _u in _udfs]
            if len(set(_names)) > 1:
                _warnings.append(
                    "openfused: "
                    + str(len(_udfs))
                    + " @fused.udf functions registered ("
                    + ", ".join(sorted(set(_names)))
                    + "); calling the last ("
                    + _names[-1]
                    + "). Multiple distinct @fused.udf entrypoints are deprecated - "
                    "pass `entrypoint` or decorate exactly one function."
                )
            _udf_fn = _unwrap_udf(_udfs[-1])
            try:
                _bound = _bind_params(_udf_fn, _load_params())
                with (
                    contextlib.redirect_stdout(_stdout_buf),
                    contextlib.redirect_stderr(_stderr_buf),
                ):
                    _return_value = _udf_fn(**_bound)
            except Exception:
                _error = traceback.format_exc()
                _stderr_buf.write(_error)
                _return_value = None
    # Classify and serialize the result by kind (spec/caching/README.md § Serialization
    # boundary): only this subprocess holds the live object, so it produces the
    # stored body. `binary` (a fused.Response, detected by the marker attribute —
    # never by value, so a plain dict can't be mistaken for one) writes its raw
    # bytes to a body file; `parquet` (a DataFrame / GeoDataFrame / pyarrow table)
    # serializes to Parquet and derives the inline value by reading that body back
    # (so a cold run and a later cache hit pass through the same round-trip and
    # cannot disagree); everything else is `json`. Wrapped so a malformed object
    # or a non-serializable value yields a clean error envelope, not a crash.
    _response = None
    _encoded_rv = None
    _kind = "json"
    _body_bytes = None
    try:
        if getattr(_return_value, "__openfused_response__", False):
            _raw = _return_value.body
            # Only a *bytes* body is the `binary` kind (raw body stored, base64
            # echo). A str/None body stays `json`: its return_value is the value
            # itself and the response meta rides in the json envelope — keeping
            # the inline echo identical between a cold run and a cache hit.
            _is_bytes = isinstance(_raw, bytes)
            if _is_bytes:
                _kind = "binary"
                _body_bytes = _raw
                _encoded_rv = json.dumps(base64.b64encode(_raw).decode())
            else:
                _encoded_rv = json.dumps(_raw) if _raw is not None else None
            _response = {
                "media_type": _return_value.media_type,
                "status_code": _return_value.status_code,
                "headers": _return_value.headers or {},
                "body_encoding": "base64" if _is_bytes else None,
            }
        elif _is_tabular(_return_value):
            try:
                _body_bytes, _encoded_rv = _serialize_tabular(_return_value)
                _kind = "parquet"
            except (ImportError, ValueError, TypeError, AttributeError, OSError):
                # Parquet/geoparquet serialization unavailable → fall back to a
                # json records body rather than failing the call (per spec). Derive
                # records in a kind-agnostic way: pyarrow Table/RecordBatch expose
                # to_pylist(); pandas/geopandas frames expose to_dict("records").
                _body_bytes = None
                if hasattr(_return_value, "to_pylist"):
                    _records = _return_value.to_pylist()
                else:
                    _records = _return_value.to_dict(orient="records")
                _encoded_rv = json.dumps(_records, default=str)
        else:
            _encoded_rv = json.dumps(_return_value) if _return_value is not None else None
    except (AttributeError, TypeError, ValueError, ImportError):
        _serialize_err = traceback.format_exc()
        _stderr_buf.write(_serialize_err)
        _error = _error or _serialize_err
        _encoded_rv = None
        _response = None
        _kind = "json"
        _body_bytes = None
    if _body_bytes is not None and _error is None:
        with open(os.path.join(os.path.dirname(out_path), "_result_body"), "wb") as _bf:
            _bf.write(_body_bytes)
    else:
        _kind = "json"
    _result = {
        "stdout": _stdout_buf.getvalue(),
        "stderr": _stderr_buf.getvalue(),
        "return_value": _encoded_rv,
        "error": _error,
        "response": _response,
        "kind": _kind,
    }
    if _warnings:
        _result["warnings"] = _warnings
    with open(out_path, "w") as _f:
        json.dump(_result, _f)


main()
