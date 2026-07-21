"""Runs a Python file through the fused local compute backend, when installed.

Re-introduction of the D55-era engine (rolled back in D67) with a different
posture (D69): the fused engine is **optional**. When the `fused` package is
importable, /api/run executes code through `LocalPythonComputeBackend` —
fresh subprocess per call in a temp exec dir, PEP 723 inline requirements
resolved into a cached venv, params delivered via `_params.json`. When it is
not installed, the built-in executor (`executor.py`/`_child.py`) runs
unchanged. `available()` is the probe; `server.py` picks per process.

Code contract under this engine (the fused contract, plus a compat bridge):

  * a function decorated with ``@fused.udf`` — **any name**; the last decorated
    one is the entrypoint and receives params as raw JSON values (no
    annotation coercion: the calling JS owns types);
  * or a plain script that assigns ``result = ...``;
  * or — compat bridge, so pages and the built-in templates run identically
    under either engine — a bare ``main()``, called with the same
    annotation-driven string coercion the built-in executor applies.

The wire shape returned here is the built-in executor's
``{ok, result, error: {type, message, traceback}, stdout}`` (plus additive
``stderr``/``duration_ms`` keys), so runtime.js and every template consume one
shape regardless of which engine ran the code.
"""
import json
import logging
import os
import re
import traceback

logger = logging.getLogger(__name__)

# PEP 723 reference regex (verbatim from the spec) for inline script metadata.
_PEP723_BLOCK = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)

# A traceback frame header: `  File "<path>", line N[, in func]`. SyntaxError
# frames have no `, in func` part.
_FRAME_LINE = re.compile(
    r'^  File "(?P<file>[^"]*)", line (?P<line>\d+)(?P<rest>, in (?P<func>\S+))?'
)

_backend = None

# Installed into every script's venv on top of its PEP 723 dependencies.
# Mirrors the `bundled` extra in pyproject.toml (SPEC DM-2) so the built-in
# template readers (pyarrow, openpyxl) work under this engine without inline
# headers. Keep the two lists in sync.
DEFAULT_REQUIREMENTS = [
    "numpy",
    "pandas",
    "pyarrow",
    "requests",
    "duckdb",
    "polars",
    "matplotlib",
    "scipy",
    "pillow",
    "openpyxl",
    "shapely",
    "geopandas",
]


def available() -> bool:
    """True iff the fused local backend is importable in this process.

    Import failure of any flavor (package absent, too-old Python, a broken
    install) means "not available" — the caller falls back to the built-in
    executor rather than surfacing an import error to every /api/run.
    """
    try:
        from fused.agent_core.backends.local import python_compute  # noqa: F401
    except ImportError:
        return False
    return True


def get_backend():
    # Lazy singleton: importing the backend pulls in the fused package tree,
    # and constructing it is only needed once per server process. 30s matches
    # the built-in executor's per-run timeout.
    global _backend
    if _backend is None:
        from fused.agent_core.backends.local.python_compute import LocalPythonComputeBackend

        # cache_storage=None disables result caching explicitly (PY-9: fresh
        # execution every call). It is the upstream default today, but we may
        # track a nightly wheel — don't rely on a default staying put.
        _backend = LocalPythonComputeBackend(timeout_seconds=30, cache_storage=None)
    return _backend


def script_requirements(text: str) -> list[str]:
    """Extract PEP 723 `dependencies` from a script's inline metadata block.

    Returns [] when there is no `# /// script` block. Malformed TOML raises
    ValueError with the parse error so the caller can surface it to the page
    instead of 500ing.
    """
    for match in _PEP723_BLOCK.finditer(text):
        if match.group("type") != "script":
            continue
        # Imported here, not at function top: tomllib is 3.11+, but this
        # function must still return [] on 3.10 for the (overwhelmingly
        # common) case of a script with no PEP 723 block at all — run_python
        # calls this unconditionally, regardless of which engine is active.
        import tomllib

        content = "".join(
            line[2:] if line.startswith("# ") else line[1:]
            for line in match.group("content").splitlines(keepends=True)
        )
        try:
            meta = tomllib.loads(content)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(
                f"invalid TOML in '# /// script' block: {e}. "
                "Fix the inline metadata header (PEP 723) or remove the block."
            ) from None
        deps = meta.get("dependencies", [])
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise ValueError(
                "'dependencies' in the '# /// script' block must be a list of strings"
            )
        return deps
    return []


def build_code(user_code: str, script_dir: str, script_path: str = "script") -> str:
    """Wrap user code so its imports/data paths resolve next to the .py, and
    bridge the bare-``main()`` contract.

    The user's source is embedded as a literal and ``exec``'d as **its own
    compile unit under its real filename** — so a leading ``from __future__``
    import stays the first statement of its unit, and every user traceback
    frame carries the real file and exact line (no offset bookkeeping). The
    wrapper must NOT chdir before the user code runs: the backend's runner
    reads _params.json from the exec cwd after module-level code finishes, so
    cwd stays on the exec dir until an entrypoint is actually invoked. The
    epilogue then:

      * wraps a registered ``@fused.udf`` function so the chdir to the
        script's dir happens just before it runs (relative data paths resolve
        against the script, params still get found);
      * otherwise — compat bridge — if the module defines a bare ``main()``,
        reads ``_params.json`` itself, coerces string params by ``main``'s
        annotations (same table as the built-in executor's worker), chdirs,
        and sets ``result = main(**bound)``. ``main()`` wins even if the
        module also assigned a module-level ``result`` — the built-in
        executor's worker (``_child.py``) always calls ``main(**params)`` and
        overwrites whatever ``result`` the module set, so a file defining both
        must behave identically under either engine;
      * otherwise, if the module set ``result`` itself, leaves it untouched;
      * otherwise raises the built-in executor's "no callable 'main'" error
        (extended with the fused-contract alternatives), so a file with no
        entrypoint fails identically under either engine.
    """
    preamble = (
        f"import os as _fused_os, sys as _fused_sys\n"
        f"_fused_sys.path.insert(0, {script_dir!r})\n"
        f"exec(compile({user_code!r}, {script_path!r}, 'exec'), globals())\n"
    )
    epilogue = f"""
try:
    import fused as _fused_shim
    _fused_udfs = getattr(_fused_shim, "_registered_udfs", None)
except ImportError:
    _fused_udfs = None
if _fused_udfs:
    _fused_udf = _fused_udfs[-1]
    _fused_inner = _fused_udf._fn
    def _fused_chdir_call(*_a, **_k):
        _fused_os.chdir({script_dir!r})
        return _fused_inner(*_a, **_k)
    _fused_udf._fn = _fused_chdir_call
else:
    import inspect as _fused_inspect
    import json as _fused_json

    def _fused_coerce(_value, _ann):
        if _ann is _fused_inspect.Parameter.empty:
            return _value
        try:
            if _ann is bool:
                if isinstance(_value, bool):
                    return _value
                if isinstance(_value, str):
                    return _value.strip().lower() in ("1", "true", "yes", "on")
                return bool(_value)
            if _ann in (int, float, str) and not isinstance(_value, _ann):
                return _ann(_value)
        except (TypeError, ValueError) as _e:
            raise TypeError("could not convert param to " + _ann.__name__ + ": " + str(_e))
        return _value

    def _fused_bind(_fn, _params):
        try:
            _sig = _fused_inspect.signature(_fn, eval_str=True)
        except (NameError, TypeError):
            _sig = _fused_inspect.signature(_fn)
        _has_var_kw = any(
            _p.kind is _fused_inspect.Parameter.VAR_KEYWORD for _p in _sig.parameters.values()
        )
        _kwargs = {{}}
        for _nm, _p in _sig.parameters.items():
            if _p.kind in (
                _fused_inspect.Parameter.VAR_KEYWORD,
                _fused_inspect.Parameter.VAR_POSITIONAL,
            ):
                continue
            if _nm in _params:
                _kwargs[_nm] = _fused_coerce(_params[_nm], _p.annotation)
            elif _p.default is _fused_inspect.Parameter.empty:
                raise TypeError("missing required param: " + repr(_nm))
        if _has_var_kw:
            for _k, _v in _params.items():
                if _k not in _kwargs:
                    _kwargs[_k] = _v
        return _kwargs

    def _fused_run_main():
        _fn = globals().get("main")
        if not callable(_fn):
            if "result" in globals():
                return globals()["result"]
            raise AttributeError(
                _fused_os.path.basename({script_path!r})
                + " does not define a callable 'main' function, a "
                "@fused.udf-decorated function, or a 'result' variable"
            )
        _params = {{}}
        _pf = _fused_os.path.join(_fused_os.getcwd(), "_params.json")
        if _fused_os.path.exists(_pf):
            with open(_pf) as _f:
                _params = _fused_json.load(_f) or {{}}
        _fused_os.chdir({script_dir!r})
        return _fn(**_fused_bind(_fn, _params))

    result = _fused_run_main()
"""
    return preamble + epilogue


def _clean_error(error_text: str, script_path: str) -> str:
    """Drop plumbing frames so a traceback starts at the user's real file.

    User code runs as its own compile unit under its real filename
    (build_code), so its frames already carry the script's path and exact
    lines — nothing needs rewriting. What remains is noise around them:
    backend internals (_runner.py, the fused shim) above the first user frame,
    and the "<lambda_exec>" wrapper/epilogue frames (the exec() trampoline and
    the bare-main bridge helpers). This drops those. Text with no
    "<lambda_exec>" frame (timeouts, backend messages) passes through
    unchanged — a raw traceback beats a mangled one.
    """
    if '  File "<lambda_exec>"' not in error_text:
        return error_text
    try:
        out = []
        seen_user_frame = False
        dropping = False  # inside a frame we decided to drop
        for line in error_text.splitlines():
            m = _FRAME_LINE.match(line)
            if m:
                if m.group("file") == script_path:
                    seen_user_frame = True
                    dropping = False
                    out.append(line)
                    continue
                # <lambda_exec> (wrapper/epilogue) frames are always plumbing;
                # other files above the first user frame are backend internals.
                # Frames below it (user code calling into libraries) are kept.
                dropping = m.group("file") == "<lambda_exec>" or not seen_user_frame
                if not dropping:
                    out.append(line)
                continue
            # Source/caret lines belong to the frame above them; anything not
            # indented (header, exception message, chain separators) is kept.
            if line.startswith("    ") and dropping:
                continue
            out.append(line)
        return "\n".join(out) + ("\n" if error_text.endswith("\n") else "")
    except (ValueError, AttributeError):
        return error_text


def _error_dict(err_type: str, message: str, tb: str = "") -> dict:
    # The built-in executor's wire shape, so all failures render uniformly.
    return {
        "ok": False,
        "error": {"type": err_type, "message": message, "traceback": tb},
        "stdout": "",
    }


def _split_error(cleaned: str) -> tuple[str, str]:
    """(type, message) from a traceback's final `SomeError: message` line.

    Falls back to ("Error", <last non-empty line>) when the text doesn't end in
    the standard form (timeouts, backend messages).
    """
    for line in reversed(cleaned.splitlines()):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_.]*)\s*:\s*(.*)$", line)
        if m and (m.group(1).endswith("Error") or m.group(1).endswith("Exception")):
            return m.group(1), m.group(2)
        return "Error", line
    return "Error", cleaned.strip() or "execution failed"


async def run_python(path: str, params: dict) -> dict:
    if not os.path.isfile(path):
        return _error_dict("FileNotFoundError", f"no such Python file: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            user_code = f.read()
    except OSError as e:
        return _error_dict("OSError", f"cannot read {path}: {e}")

    try:
        reqs = script_requirements(user_code)
    except ValueError as e:
        return _error_dict("ValueError", str(e))

    # Sorted union so the venv cache key is stable regardless of how a script
    # orders its PEP 723 block; scripts with no block all share one defaults venv.
    requirements = sorted(set(DEFAULT_REQUIREMENTS) | set(reqs))

    abs_path = os.path.abspath(path)
    code = build_code(user_code, os.path.dirname(abs_path), abs_path)
    try:
        r = await get_backend().execute(
            code=code,
            requirements=requirements,
            input_files={"_params.json": json.dumps(params or {}).encode()},
        )
    except Exception:
        # The backend itself blew up (import failure, venv/dep resolution,
        # subprocess spawn…) — not the user's script. Return the same wire
        # shape as every other failure so the page's error overlay (D17)
        # shows the full traceback, and log it so the log file has it too.
        logger.exception("fused engine execute failed for %s", path)
        return _error_dict(
            "EngineError",
            f"fused-render internal error (not your script) while running {path}",
            traceback.format_exc(),
        )

    if r.error:
        cleaned = _clean_error(r.error, abs_path)
        err_type, message = _split_error(cleaned)
        return {
            "ok": False,
            "error": {"type": err_type, "message": message, "traceback": cleaned},
            "stdout": r.stdout,
            "stderr": r.stderr,
            "duration_ms": r.duration_ms,
        }

    # The backend hands return_value back JSON-encoded; decode it here so the
    # wire carries real values ({"x": 1}, not "{\"x\": 1}"). Base64 binary
    # bodies stay strings, and anything that isn't valid JSON passes through.
    # parse_constant: python's json accepts NaN/Infinity/-Infinity and would
    # decode them to floats that the response serializer re-emits as bare NaN,
    # which the browser's strict JSON.parse rejects — the whole /api/run
    # response would fail to parse. Decode them as their literal names instead.
    return_value = r.return_value
    if isinstance(return_value, str) and not (r.response and r.response.body_encoding == "base64"):
        try:
            return_value = json.loads(return_value, parse_constant=lambda c: c)
        except ValueError:
            pass
    return {
        "ok": True,
        "result": return_value,
        "stdout": r.stdout,
        "stderr": r.stderr,
        "duration_ms": r.duration_ms,
    }
