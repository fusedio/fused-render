# Derived from openfused (MIT, Copyright (c) 2026 Fused Labs, Inc.); see NOTICE.
"""Signature → MCP tool schema for the app MCP assembly.

An MCP assembly that publishes *user Python* as tools faces a problem:
turn a function's parameter list into a JSON schema **without importing the
module** (importing project or app code would run its top-level statements — a
read must never execute the thing it describes).

The answer is here: read the parameters off the AST
(`params_from_funcdef`), then hand FastMCP a synthetic callable carrying exactly
that signature (`synthesize_callable`) so its own `Tool.from_function`
signature → JSON-schema machinery produces the schema. Consumer:

* `app_mcp.py` — one tool per curated `[[tool]]`, minus its pinned parameters
  (spec/serve/app-mcp.md §4).

The mapping this yields — no default → required; a literal default → optional
carrying that default; a simple annotation → the obvious JSON type; anything
else → a free JSON value — is the contract both specs describe, so it lives in
one place rather than being re-derived (and drifting) per assembly.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from typing import Any

# Simple annotation-name → Python type map. An annotation outside this set (or
# absent) is left unannotated so FastMCP exposes the parameter as a free JSON
# value carrying the name (and description): `list[str]` / `Optional[...]` /
# custom types are deliberately not translated rather than guessed at.
SIMPLE_TYPES: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
}

# Marker for "has a default, but its value is not statically knowable" — see
# `literal_default`.
OPAQUE_DEFAULT = object()


@dataclass(frozen=True)
class ParamSpec:
    """One derived tool parameter.

    ``has_default``/``default`` drive required-vs-optional; ``annotation`` is a
    Python type from ``SIMPLE_TYPES`` or ``None`` for an unannotated/unmappable
    parameter (exposed as a free JSON value). A named structure rather than a
    tuple per the repo's no-tuple-returns convention.
    """

    name: str
    annotation: type | None = None
    has_default: bool = False
    default: Any = None


def literal_default(node: ast.expr) -> Any:
    """Best-effort static value of a default expression, or `OPAQUE_DEFAULT`.

    Parameter derivation never executes the code it reads, so a default is taken
    only when it is a literal. A non-literal default (a call, a name) is treated
    as "has a default but its value is opaque" — the JSON schema then omits the
    concrete default while still marking the parameter optional. The sentinel
    distinguishes that from a real ``None`` default.
    """
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return OPAQUE_DEFAULT


def annotation_type(node: ast.expr | None) -> type | None:
    """Map a simple annotation node to a Python type, else ``None``."""
    if isinstance(node, ast.Name):
        return SIMPLE_TYPES.get(node.id)
    return None


def params_from_funcdef(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ParamSpec]:
    """Derive ``ParamSpec``s from a function def's arguments.

    Positional-or-keyword and keyword-only args are exposed; ``*args``/
    ``**kwargs`` are skipped (an MCP tool exposes named parameters). Defaults
    align right per Python semantics: the last ``len(defaults)`` positional args
    carry them.
    """
    args = fn.args
    specs: list[ParamSpec] = []

    positional = args.posonlyargs + args.args
    defaults = args.defaults
    offset = len(positional) - len(defaults)
    for i, arg in enumerate(positional):
        if i >= offset:
            value = literal_default(defaults[i - offset])
            specs.append(
                ParamSpec(
                    name=arg.arg,
                    annotation=annotation_type(arg.annotation),
                    has_default=True,
                    default=None if value is OPAQUE_DEFAULT else value,
                )
            )
        else:
            specs.append(
                ParamSpec(
                    name=arg.arg,
                    annotation=annotation_type(arg.annotation),
                    has_default=False,
                )
            )

    for arg, default_node in zip(args.kwonlyargs, args.kw_defaults):
        if default_node is None:
            specs.append(
                ParamSpec(
                    name=arg.arg, annotation=annotation_type(arg.annotation), has_default=False
                )
            )
        else:
            value = literal_default(default_node)
            specs.append(
                ParamSpec(
                    name=arg.arg,
                    annotation=annotation_type(arg.annotation),
                    has_default=True,
                    default=None if value is OPAQUE_DEFAULT else value,
                )
            )
    return specs


def funcdef_named(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The top-level ``def name`` in ``tree``, or ``None``.

    Top-level only: an MCP tool's entrypoint must be reachable by a plain name
    lookup in the executed module's namespace (that is what the runner does with
    an explicit ``entrypoint``), which a nested or class-scoped def is not.
    """
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def synthesize_callable(
    params: list[ParamSpec],
    name: str,
    doc: str,
    handler: Any,
) -> Any:
    """A callable carrying ``params`` as its real signature, forwarding to ``handler``.

    FastMCP derives a tool's input schema from the registered callable's
    signature (``Tool.from_function``), so rather than hand-building JSON schema
    we synthesize a function whose signature *is* the derived parameter list
    (names, annotations, defaults) and let FastMCP do the mapping. Bound
    arguments — defaults applied — are handed to ``handler`` as a single dict, so
    a tool handler never re-does the binding.
    """
    parameters: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    for p in params:
        default = p.default if p.has_default else inspect.Parameter.empty
        parameters.append(
            inspect.Parameter(
                p.name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=default,
                annotation=p.annotation if p.annotation is not None else inspect.Parameter.empty,
            )
        )
        if p.annotation is not None:
            annotations[p.name] = p.annotation
    signature = inspect.Signature(parameters)

    async def _tool(*args: Any, **kwargs: Any) -> Any:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        return await handler(dict(bound.arguments))

    setattr(_tool, "__signature__", signature)
    _tool.__annotations__ = annotations
    _tool.__name__ = name
    _tool.__doc__ = doc
    return _tool
