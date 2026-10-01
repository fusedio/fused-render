"""Static (AST-only) description of the `.py` files beside an app's page.

Answers "what could a caller run here, with which parameters?" for
`GET /api/apps/python` (routers/app_python.py) — the discovery half of letting
a bot call an app's Python directly through the same `POST /api/run` the page
uses (SPEC §49). Everything here is an `ast.parse` of the file's text; nothing
is ever imported or executed, because top-level code in these folders touches
token files and local state and a *listing* must never run it.

`templates/mcp/inspect_app.py` carries its own copy of the same formatter
(`_signature`, `_params`, `_entrypoints`): that file is a template backend and
may not import `fused_render` (SPEC PY-15), while this one runs inside the
server. The two are independent — discovery records no signature snapshot for
drift, so they need not agree byte for byte — but keep their *shape* aligned
so a bot and the MCP panel describe one function the same way.

stdlib only. Nothing here raises for an unreadable or unparseable file: that
is reported per file, so one half-written sibling never blanks the listing.
"""
from __future__ import annotations

import ast
import os

#: Only the folder's own top level is listed — the same rule `inspect_app`
#: and `/api/run`'s callers follow: a `.py` the page could `runPython`.
MAX_FILES = 60
#: Bounded read: a listing must not stall on a multi-megabyte data file that
#: happens to end in `.py`.
READ_LIMIT = 512 * 1024
#: The one entrypoint `/api/run` calls (`_child.py` binds `main(**params)`).
ENTRYPOINT = "main"


def first_line(doc) -> str:
    """A docstring's first non-empty line, or `""`."""
    for line in (doc or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def params(fn) -> list[dict]:
    """One entry per named parameter: name, annotation, default, required.
    `*args`/`**kwargs` are skipped (nothing a caller can name). Annotation and
    default are TEXT (`ast.unparse`), never evaluated values."""
    out = []
    positional = list(fn.args.posonlyargs) + list(fn.args.args)
    defaults = list(fn.args.defaults)
    offset = len(positional) - len(defaults)
    for i, arg in enumerate(positional):
        default = defaults[i - offset] if i >= offset else None
        out.append(_param(arg, default))
    for arg, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults):
        out.append(_param(arg, default))
    return out


def _param(arg, default) -> dict:
    return {
        "name": arg.arg,
        "type": ast.unparse(arg.annotation) if arg.annotation else "",
        "default": ast.unparse(default) if default is not None else "",
        "required": default is None,
    }


def signature(name: str, fn) -> str:
    return "%s(%s)" % (name, ast.unparse(fn.args))


def funcdef_named(tree, name: str):
    """The top-level (sync or async) function called `name`, or None."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def python_files(folder: str) -> list[str]:
    """Top-level, non-hidden `.py` names, sorted, capped at MAX_FILES."""
    try:
        with os.scandir(folder) as entries:
            names = sorted(
                e.name for e in entries
                if e.name.endswith(".py") and not e.name.startswith(".") and e.is_file()
            )
    except OSError:
        return []
    return names[:MAX_FILES]


def file_report(folder: str, name: str) -> dict:
    """One `.py` as a caller sees it.

    `callable` is true only when a top-level sync `main` exists — the single
    entrypoint `/api/run` binds (an `async def main` is listed, not callable). Other public functions are listed under
    `functions` (names only) so a bot can tell "helper module" from "has a
    `send()` the MCP panel could curate", without pretending `/api/run`
    could reach them. An unparseable file is reported with its error, not
    dropped: the error is what tells the caller why the file offers nothing.
    """
    entry = {"file": name, "callable": False, "doc": "", "params": [],
             "signature": "", "functions": [], "error": ""}
    try:
        with open(os.path.join(folder, name), "r", encoding="utf-8", errors="replace") as fh:
            source = fh.read(READ_LIMIT)
    except OSError as exc:
        entry["error"] = str(exc)
        return entry
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        entry["error"] = "%s: %s" % (type(exc).__name__, exc)
        return entry
    entry["functions"] = [
        n.name for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")
    ]
    fn = funcdef_named(tree, ENTRYPOINT)
    if fn is None:
        entry["doc"] = first_line(ast.get_docstring(tree))
        entry["reason"] = "no top-level main()"
        return entry
    if isinstance(fn, ast.AsyncFunctionDef):
        # `_child.py` calls main() and JSON-dumps the return; a coroutine would
        # come back as "not serialisable". Listed, named, not callable.
        entry["doc"] = first_line(ast.get_docstring(fn)) or first_line(ast.get_docstring(tree))
        entry["reason"] = "async def main (/api/run calls a sync main)"
        return entry
    entry["callable"] = True
    # The function's own docstring describes the call; the module's is the
    # fallback for a file whose author documented only the top.
    entry["doc"] = first_line(ast.get_docstring(fn)) or first_line(ast.get_docstring(tree))
    entry["params"] = params(fn)
    entry["signature"] = signature(ENTRYPOINT, fn)
    return entry


def folder_report(folder: str) -> list[dict]:
    """`file_report` for every listed `.py` in `folder`, in name order."""
    return [file_report(folder, name) for name in python_files(folder)]
