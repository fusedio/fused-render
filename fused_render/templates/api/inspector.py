"""Inspector backing api/template.html. Statically parses a target .py via
`ast` — never imports or executes it — and returns the shape the form UI
needs: module docstring, PEP 723 dependencies, and the entrypoint function's
signature (params, annotations, defaults, docstring). Stdlib only.

Entrypoint resolution mirrors the execution engines (D68): a function
decorated with ``@fused.udf`` wins (any name; the **last** decorated one, the
same pick the fused engine makes), else a bare ``main()``.
"""
import ast


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


def _find_entrypoint(tree):
    """The last ``@fused.udf``-decorated function, else a bare ``main()``."""
    decorated = None
    main_fn = None
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(_is_fused_udf_decorator(d) for d in node.decorator_list):
            decorated = node  # last one wins, matching the engine's pick
        elif node.name == "main":
            main_fn = node
    return decorated or main_fn


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


def main(file: str) -> dict:
    with open(file, encoding="utf-8", errors="replace") as f:
        source = f.read()
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return {"parse_error": f"line {e.lineno}: {e.msg}"}

    fn = _find_entrypoint(tree)
    result = {
        "parse_error": None,
        "module_docstring": ast.get_docstring(tree),
        "dependencies": [],
        "function": None,
    }
    if fn is not None:
        result["function"] = {
            "name": fn.name,
            "docstring": ast.get_docstring(fn),
            "params": _params(fn),
        }
    return result
