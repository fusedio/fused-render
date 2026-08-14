"""Inspector backing api/template.html. Statically parses a target .py via
`ast` — never imports or executes it — and returns the shape the form UI
needs: module docstring, the dependencies its PROJECT FOLDER declares (fused
engine only — see `dependencies` below), and the entrypoint function's signature
(params, annotations, defaults, docstring). Stdlib only.

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
import os


def _project_root(file: str):
    """The project folder whose environment *file* runs in, or None.

    The topmost ancestor holding a ``pyproject.toml`` — `projectenv`'s rule for
    everything outside an app folder or a template folder, which are the two
    containers this module cannot know about: templates must reach the app only
    through ``templates/shared/appenv.py`` (SPEC PY-15, D166), so importing
    ``fused_render.projectenv`` — the authoritative derivation — is not allowed
    here, and is pinned shut by `test_no_template_imports_fused_render`.

    That makes this the one place the boundary rule is restated, and the reason
    that is tolerable is that it is DISPLAY-ONLY: the engine never consults it,
    so the worst a divergence costs is a label naming an ancestor of the real
    root. Inside a container the two agree whenever the container is the topmost
    declaring folder, which is the layout every app and template has.
    """
    found = None
    d = os.path.dirname(os.path.abspath(file))
    while True:
        if os.path.isfile(os.path.join(d, "pyproject.toml")):
            found = d
        parent = os.path.dirname(d)
        if parent == d:
            return found
        d = parent


def _project_dependencies(root) -> list:
    """``[project].dependencies`` of *root*'s ``pyproject.toml``.

    Never raises: this is a read-only display, not something that should break
    the whole inspector view over a malformed manifest or a pre-3.11 interpreter
    (``tomllib``) — either quietly yields ``[]``.
    """
    if not root:
        return []
    try:
        import tomllib
    except ImportError:
        return []
    try:
        with open(os.path.join(root, "pyproject.toml"), "rb") as f:
            meta = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    project = meta.get("project")
    if not isinstance(project, dict):
        return []
    deps = project.get("dependencies", [])
    if not isinstance(deps, list):
        return []
    return [d for d in deps if isinstance(d, str)]


def _ignored_nested_manifests(root, file: str) -> list:
    """``pyproject.toml`` files BELOW *root* on the path down to *file*.

    Each one is inert: the environment is the project root's, so a manifest in a
    subfolder declares nothing and installs nothing. Surfaced rather than left
    silent because a file that looks correct and does nothing is exactly the
    failure D177 was written about — the user edits it, nothing changes, and
    there is no signal anywhere connecting the two.
    """
    if not root:
        return []
    root = os.path.abspath(root)
    out = []
    d = os.path.dirname(os.path.abspath(file))
    while d.startswith(root + os.sep):
        candidate = os.path.join(d, "pyproject.toml")
        if os.path.isfile(candidate):
            out.append(candidate)
        d = os.path.dirname(d)
    return out


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

    root = _project_root(file)
    fn = _find_entrypoint(tree, engine)
    result = {
        "parse_error": None,
        "module_docstring": ast.get_docstring(tree),
        # Only the fused engine actually builds a venv from the project's
        # declaration (PY-16) — the builtin executor ignores it, so showing the
        # dependencies there would imply an install that never happens.
        "dependencies": _project_dependencies(root) if engine == "fused" else [],
        # Which folder that declaration came from, so the form can say WHERE the
        # environment is declared rather than implying the file declares it.
        "project": root if engine == "fused" else None,
        # Inert manifests below the root. See `_ignored_nested_manifests`.
        "ignored_manifests": (
            _ignored_nested_manifests(root, file) if engine == "fused" else []
        ),
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
