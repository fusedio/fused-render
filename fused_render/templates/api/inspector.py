"""Inspector backing api/template.html. Statically parses a target .py via
`ast` — never imports or executes it — and returns the shape the form UI
needs: module docstring, PEP 723 deps, and the @fused.udf function's
signature (params, annotations, defaults, docstring). Stdlib only, so the
backend's bare venv runs it without a PEP 723 header of its own.
"""
import ast
import re
import tomllib

import fused

# Same PEP 723 block grammar the engine uses (SPEC PY-6).
_PEP723 = re.compile(
    r"^# /// script\s*$(.*?)^# ///\s*$", re.MULTILINE | re.DOTALL
)


def _pep723_dependencies(source: str) -> list:
    m = _PEP723.search(source)
    if not m:
        return []
    toml_text = "\n".join(
        line[2:] if line.startswith("# ") else line[1:]
        for line in m.group(1).splitlines()
        if line.startswith("#")
    )
    try:
        return tomllib.loads(toml_text).get("dependencies", []) or []
    except tomllib.TOMLDecodeError:
        return []


def _is_udf_decorator(node) -> bool:
    """Matches @fused.udf, @udf, and their called forms @fused.udf(...)."""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return (
            node.attr == "udf"
            and isinstance(node.value, ast.Name)
            and node.value.id == "fused"
        )
    return isinstance(node, ast.Name) and node.id == "udf"


def _find_udf_function(tree):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(_is_udf_decorator(d) for d in node.decorator_list):
                return node
    return None


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


@fused.udf
def main(file: str) -> dict:
    with open(file, encoding="utf-8", errors="replace") as f:
        source = f.read()
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return {"parse_error": f"line {e.lineno}: {e.msg}"}

    fn = _find_udf_function(tree)
    result = {
        "parse_error": None,
        "module_docstring": ast.get_docstring(tree),
        "dependencies": _pep723_dependencies(source),
        "function": None,
    }
    if fn is not None:
        result["function"] = {
            "name": fn.name,
            "docstring": ast.get_docstring(fn),
            "params": _params(fn),
        }
    return result
