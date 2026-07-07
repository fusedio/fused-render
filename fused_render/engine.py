"""Runs a Python file through openfused's local compute backend.

The backend (`LocalPythonComputeBackend`) exec()s the code in a fresh
subprocess inside a temp exec dir, resolves PEP 723 inline requirements into
a cached venv, and — when the code registers a `@fused.udf` — calls the
last-registered UDF with kwargs read from `_params.json` in that exec dir.
"""
import json
import os
import re
import tomllib

# PEP 723 reference regex (verbatim from the spec) for inline script metadata.
_PEP723_BLOCK = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)

# build_code's preamble is exactly this many physical lines, so a line N in a
# "<lambda_exec>" traceback frame is line N - _PREAMBLE_LINES of the user's
# file. _clean_error relies on this; if the preamble ever grows, change both.
_PREAMBLE_LINES = 1

# A traceback frame header: `  File "<path>", line N[, in func]`. SyntaxError
# frames have no `, in func` part.
_FRAME_LINE = re.compile(r'^  File "(?P<file>[^"]*)", line (?P<line>\d+)(?P<rest>, in (?P<func>\S+))?')

_backend = None

# Installed into every script's venv on top of its PEP 723 dependencies.
# Mirrors the `bundled` extra in pyproject.toml (SPEC DM-2): the packaged
# .app's wheelhouse ships exactly these wheels (+ pyarrow), so the one-time
# venv build resolves offline. Keep the two lists in sync.
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


def get_backend():
    # Lazy singleton: importing the backend pulls in the fused package tree,
    # and constructing it is only needed once per server process. 30s matches
    # the old executor's per-run timeout.
    global _backend
    if _backend is None:
        from fused.agent_core.backends.local.python_compute import LocalPythonComputeBackend

        # cache_storage=None disables result caching explicitly (D55, PY-9:
        # fresh execution every call). It is the upstream default today, but
        # we track a nightly wheel — don't rely on a default staying put.
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


def build_code(user_code: str, script_dir: str) -> str:
    """Wrap user code so its imports and data paths resolve next to the .py.

    Preamble is ONE physical line so user tracebacks are offset by exactly 1.
    It must NOT chdir: the backend's runner reads _params.json from the exec
    cwd after module-level code finishes, so cwd has to stay on the exec dir
    until then. The epilogue instead wraps the registered UDF so the chdir to
    the script's dir happens just before main() runs — relative data paths in
    main() resolve against the script, params still get found.
    """
    preamble = (
        f"import os as _fused_os, sys as _fused_sys; "
        f"_fused_sys.path.insert(0, {script_dir!r})\n"
    )
    epilogue = f"""
try:
    import fused as _fused_shim
    _fused_udfs = getattr(_fused_shim, "_registered_udfs", None)
    if _fused_udfs:
        _fused_udf = _fused_udfs[-1]
        _fused_inner = _fused_udf._fn
        def _fused_chdir_call(*_a, **_k):
            _fused_os.chdir({script_dir!r})
            return _fused_inner(*_a, **_k)
        _fused_udf._fn = _fused_chdir_call
except ImportError:
    pass
"""
    return preamble + user_code + "\n" + epilogue


def _clean_error(error_text: str, script_path: str) -> str:
    """Rewrite a backend traceback so it points at the user's real file.

    The backend exec()s our wrapped code as "<lambda_exec>", so raw tracebacks
    lead with backend internals (_runner.py, the fused shim) and our epilogue
    wrapper (_fused_chdir_call), and their line numbers are shifted by the
    preamble. This drops the noise frames and rewrites
    `File "<lambda_exec>", line N` to the script's path at line
    N - _PREAMBLE_LINES. Text with no "<lambda_exec>" frame (timeouts, backend
    errors) and anything that doesn't parse cleanly passes through unchanged —
    a raw traceback beats a mangled one.
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
                if m.group("file") == "<lambda_exec>":
                    if m.group("func") == "_fused_chdir_call":
                        dropping = True  # our epilogue wrapper, not user code
                        continue
                    seen_user_frame = True
                    dropping = False
                    lineno = int(m.group("line")) - _PREAMBLE_LINES
                    out.append(f'  File "{script_path}", line {lineno}{m.group("rest") or ""}')
                    continue
                # Backend internals sit above the first user frame; frames below
                # it (user code calling into libraries) are kept.
                dropping = not seen_user_frame
                if not dropping:
                    out.append(line)
                continue
            # Source/caret lines belong to the frame above them; anything not
            # indented (header, exception message, chain separators) is kept.
            if line.startswith("    ") and dropping:
                continue
            out.append(line)
        return "\n".join(out) + ("\n" if error_text.endswith("\n") else "")
    except Exception:
        return error_text


def _error(message: str) -> dict:
    # Same wire shape as a real run so the page handles all failures uniformly.
    return {
        "stdout": "",
        "stderr": "",
        "return_value": None,
        "duration_ms": 0,
        "error": message,
        "response": None,
    }


async def run_python(path: str, params: dict) -> dict:
    if not os.path.isfile(path):
        return _error(f"no such Python file: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            user_code = f.read()
    except OSError as e:
        return _error(f"cannot read {path}: {e}")

    try:
        reqs = script_requirements(user_code)
    except ValueError as e:
        return _error(str(e))

    # Sorted union so the venv cache key is stable regardless of how a script
    # orders its PEP 723 block; scripts with no block all share one defaults venv.
    requirements = sorted(set(DEFAULT_REQUIREMENTS) | set(reqs))

    code = build_code(user_code, os.path.dirname(os.path.abspath(path)))
    r = await get_backend().execute(
        code=code,
        requirements=requirements,
        input_files={"_params.json": json.dumps(params or {}).encode()},
    )
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
        "stdout": r.stdout,
        "stderr": r.stderr,
        "return_value": return_value,
        "duration_ms": r.duration_ms,
        "error": _clean_error(r.error, path) if r.error else r.error,
        "response": r.response.to_wire() if r.response else None,
    }
