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

_backend = None


def get_backend():
    # Lazy singleton: importing the backend pulls in the fused package tree,
    # and constructing it is only needed once per server process. 30s matches
    # the old executor's per-run timeout.
    global _backend
    if _backend is None:
        from fused.agent_core.backends.local.python_compute import LocalPythonComputeBackend

        _backend = LocalPythonComputeBackend(timeout_seconds=30)
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

    code = build_code(user_code, os.path.dirname(os.path.abspath(path)))
    r = await get_backend().execute(
        code=code,
        requirements=reqs or None,
        input_files={"_params.json": json.dumps(params or {}).encode()},
    )
    return {
        "stdout": r.stdout,
        "stderr": r.stderr,
        "return_value": r.return_value,
        "duration_ms": r.duration_ms,
        "error": r.error,
        "response": r.response.to_wire() if r.response else None,
    }
