"""Inspector backing api/template.html. Statically parses a target .py via
`ast` — never imports or executes it — and returns the shape the form UI
needs: module docstring, PEP 723 dependencies (fused engine only — see
`dependencies` below), and the entrypoint function's signature (params,
annotations, defaults, docstring). Stdlib only.

Entrypoint resolution mirrors the **active** execution engine (D69) — the
template passes ``engine`` from ``/api/config`` so the form always describes
the function that will actually run: under the fused engine a function
decorated with ``@fused.udf`` wins (any name; the **last** decorated one, the
same pick the engine makes), else a bare ``main()``; under the builtin
executor only ``main()`` is ever called, so only it is shown.

A ``result = ...`` script (fused engine only — engine.py's compat bridge
leaves it untouched when there's no ``main``/``@fused.udf``) has no function
to describe, but it's still a runnable, parameterless entrypoint — flagged
via ``static_result`` so the template can offer Execute instead of reporting
"no main()".
"""

import ast
import re


def _pep723_dependencies(source: str) -> list:
    """Best-effort ``dependencies`` from a ``# /// script`` PEP 723 block
    (mirrors engine.py's ``script_requirements``, standalone — this module
    must stay import-independent of ``fused_render`` since it may run inside
    the fused engine's own isolated per-script venv, not the server's).

    Never raises: this is a read-only display, not something that should
    break the whole inspector view over a malformed block or a pre-3.11
    interpreter (``tomllib``) — either quietly yields ``[]``.
    """
    try:
        import tomllib
    except ImportError:
        return []
    match = re.search(r"(?m)^# /// script$\s(?P<content>(^#(| .*)$\s)+)^# ///$", source)
    if match is None:
        return []
    content = "".join(
        line[2:] if line.startswith("# ") else line[1:]
        for line in match.group("content").splitlines(keepends=True)
    )
    try:
        meta = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        return []
    deps = meta.get("dependencies", [])
    if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
        return []
    return deps


def _is_fused_udf_decorator(node) -> bool:
    # Matches `@fused.udf` and `@fused.udf(...)`.
    if isinstance(node, ast.Call):
        node = node.func
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "udf"
        and isinstance(node.value, ast.Name)
        and node.value.id == "fused"
    )


def _find_entrypoint(tree, engine: str):
    """The function the active engine will call.

    fused engine: the last ``@fused.udf``-decorated function, else a bare
    ``main()`` (the compat bridge). builtin executor: ``main()`` only.
    """
    decorated = None
    main_fn = None
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(_is_fused_udf_decorator(d) for d in node.decorator_list):
            decorated = node  # last one wins, matching the engine's pick
        elif node.name == "main":
            main_fn = node
    if engine == "fused":
        return decorated or main_fn
    return main_fn


def _has_module_result(tree) -> bool:
    """Whether the module assigns ``result`` at the top level (the fused
    engine's "leave it untouched" case — see build_code)."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "result" for t in node.targets):
                return True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "result":
                return True
    return False


def _params(fn) -> list:
    args = list(fn.args.posonlyargs) + list(fn.args.args)
    # Positional defaults align with the tail of the arg list.
    defaults = [None] * (len(args) - len(fn.args.defaults)) + list(fn.args.defaults)
    pairs = list(zip(args, defaults))
    pairs += list(zip(fn.args.kwonlyargs, fn.args.kw_defaults))

    params = []
    for arg, default in pairs:
        p = {
            "name": arg.arg,
            "annotation": ast.unparse(arg.annotation) if arg.annotation else None,
            "has_default": default is not None,
            "default": None,
            "default_repr": None,
        }
        if default is not None:
            try:
                p["default"] = ast.literal_eval(default)
            except (ValueError, SyntaxError):
                # Non-literal default (call, name, …) — show source, don't eval.
                p["default_repr"] = ast.unparse(default)
        params.append(p)
    return params


def main(file: str, engine: str = "builtin") -> dict:
    with open(file, encoding="utf-8", errors="replace") as f:
        source = f.read()
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return {"parse_error": f"line {e.lineno}: {e.msg}"}

    fn = _find_entrypoint(tree, engine)
    result = {
        "parse_error": None,
        "module_docstring": ast.get_docstring(tree),
        # Only the fused engine actually resolves PEP 723 deps into a venv
        # (PY-12) — the builtin executor ignores them, so showing them there
        # would imply an install that never happens.
        "dependencies": _pep723_dependencies(source) if engine == "fused" else [],
        "function": None,
        "static_result": False,
    }
    if fn is not None:
        result["function"] = {
            "name": fn.name,
            "docstring": ast.get_docstring(fn),
            "params": _params(fn),
        }
    elif engine == "fused" and _has_module_result(tree):
        result["static_result"] = True
    return result
