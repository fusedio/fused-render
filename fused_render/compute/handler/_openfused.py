# Derived from openfused (MIT, Copyright (c) 2026 Fused Labs, Inc.); see ../NOTICE.
# _WORK_DIR is injected as the leading line of this file by handler._write_sdk()
# when it is written to the execution directory.
import json as _json
import os as _os
import uuid as _uuid

# Fan-out: these point at the shared directory the host dispatcher watches (see
# fused_render/compute/invoke.py).
_LOCAL_INVOKE_DIR = _os.environ.get("OPENFUSED_LOCAL_INVOKE_DIR")
# The interpreter the parent call ran on, so children see the same dependencies.
_LOCAL_WORKFLOW_INTERPRETER = _os.environ.get("OPENFUSED_LOCAL_WORKFLOW_INTERPRETER")
_INVOKE_DEPTH = int(_os.environ.get("OPENFUSED_INVOKE_DEPTH") or "0")
_MAX_INVOKE_DEPTH = int(_os.environ.get("OPENFUSED_INVOKE_MAX_DEPTH") or "5")
_LOCAL_INVOKE_TIMEOUT = float(_os.environ.get("OPENFUSED_LOCAL_INVOKE_TIMEOUT") or "900")
_INTERNAL = {
    "user_code.py",
    "_runner.py",
    "_result.json",
    "_openfused.py",
    "_params.json",
    "test_user_code.py",
    "junit.xml",
    "coverage.json",
    "fused.py",
    "openfused.py",
}

_params_path = _os.path.join(_WORK_DIR, "_params.json")
args = _json.load(open(_params_path)) if _os.path.exists(_params_path) else {}


def invoke(filename, input_files=None, async_=False, cache_max_age="0s", **kwargs):
    with open(_os.path.join(_WORK_DIR, filename)) as _f:
        _code = _f.read()

    _child_files = {}
    for _fname in _os.listdir(_WORK_DIR):
        _fpath = _os.path.join(_WORK_DIR, _fname)
        if _fname not in _INTERNAL and _fname != filename and _os.path.isfile(_fpath):
            with open(_fpath, "rb") as _f:
                _child_files[_fname] = _f.read()
    if kwargs:
        # sort_keys: identical kwargs in any insertion order give identical bytes.
        _child_files["_params.json"] = _json.dumps(kwargs, sort_keys=True).encode()
    if input_files:
        for _n, _d in input_files.items():
            _child_files[_n] = _d if isinstance(_d, bytes) else _d.encode()

    # `cache_max_age` is accepted and ignored: results are never cached here.
    return _invoke_local(_code, _child_files, async_)


def _invoke_local(code, child_files, async_):
    # Local fan-out path: write a request into the shared dir the host dispatcher
    # watches, then poll for its result.
    import base64 as _b64
    import time as _time

    if _INVOKE_DEPTH >= _MAX_INVOKE_DEPTH:
        raise RuntimeError(
            "openfused: max local invoke depth ("
            + str(_MAX_INVOKE_DEPTH)
            + ") exceeded; check for unbounded recursive fan-out"
        )

    _id = _uuid.uuid4().hex
    _req = {
        "id": _id,
        "code": code,
        "depth": _INVOKE_DEPTH + 1,
        "files": {_n: _b64.b64encode(_d).decode() for _n, _d in child_files.items()},
        "async": async_,
    }
    if _LOCAL_WORKFLOW_INTERPRETER:
        _req["interpreter"] = _LOCAL_WORKFLOW_INTERPRETER
    _req_dir = _os.path.join(_LOCAL_INVOKE_DIR, "requests")
    _tmp = _os.path.join(_req_dir, _id + ".json.tmp")
    _final = _os.path.join(_req_dir, _id + ".json")
    with open(_tmp, "w") as _f:
        _json.dump(_req, _f)
    _os.replace(_tmp, _final)

    if async_:
        # Fire-and-forget: the dispatcher will run it; child failures are not
        # surfaced here.
        return None

    _res_path = _os.path.join(_LOCAL_INVOKE_DIR, "results", _id + ".json")
    _deadline = _time.time() + _LOCAL_INVOKE_TIMEOUT
    while _time.time() < _deadline:
        if _os.path.exists(_res_path):
            with open(_res_path) as _f:
                _body = _json.load(_f)
            _error = _body.get("error")
            _rv = _body.get("return_value")
            if _rv is not None:
                try:
                    _rv = _json.loads(_rv)
                except (ValueError, TypeError):
                    pass
            if _error and _rv is None:
                raise RuntimeError("Child execution failed:" + chr(10) + _error)
            return {
                "stdout": _body.get("stdout", ""),
                "stderr": _body.get("stderr", ""),
                "return_value": _rv,
                "error": _error,
                "response": _body.get("response"),
            }
        _time.sleep(0.05)
    raise RuntimeError(
        "openfused: local child invocation timed out after " + str(_LOCAL_INVOKE_TIMEOUT) + "s"
    )
