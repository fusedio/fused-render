"""Does running this UDF need the user's say-so first? (SPEC §53.5 NU-6.)

The hosted workbench never runs some UDFs on its own — one whose decorator
names an engine, one that starts batch work from inside, one that fans out
with `.map()` — and asks before each run of one. A node's page here runs the
same file in the same Fused, so it asks by the same rules. This module is
those rules: the workbench's classifier, kept rule for rule so the two agree
on which UDFs are which.

`execution_type(code)` returns one of:

  "none"             runs freely
  "decorator-batch"  `@fused.udf(engine=…)` / `(instance_type=…)`: a batch job
  "inner-batch"      a UDF called, or `fused.run` / `fused.submit`, with a
                     batch engine inside the code
  "map"              `.map(…)` on a UDF

Two passes, in the workbench's order:

1. A text match on the first `@fused.udf(…)`: an `engine=` or
   `instance_type=` among its arguments makes it "decorator-batch" WHATEVER
   the value (`engine="realtime"` included). That is the workbench's rule, and
   it errs toward asking.
2. The syntax tree: a decorator kwarg with a batch value; `.map(` on a name
   that is a `@fused.udf` function or a `fused.load(…)` result; a direct call
   of such a name, or `fused.run` / `fused.submit`, with a batch value.

Code that does not parse is "none": the run itself reports the syntax error.

Stdlib only, no I/O, nothing is executed: the code is read as text and as a
tree.
"""
from __future__ import annotations

import ast
import re

NONE = "none"
DECORATOR_BATCH = "decorator-batch"
INNER_BATCH = "inner-batch"
MAP = "map"

#: The prompt's title for each kind (the workbench's own wording).
CONFIRM_TITLES = {
    DECORATOR_BATCH: "Confirm batch job run",
    INNER_BATCH: "Confirm batch operation",
    MAP: "Confirm map operation",
}
CONFIRM_TITLE_DEFAULT = "Confirm execution"

#: Why such a UDF did not run on its own (the workbench's Run tooltips).
PAUSED_REASONS = {
    DECORATOR_BATCH: "Auto-execution is disabled for batch job UDFs",
    INNER_BATCH: "Auto-execution is disabled for UDFs with inner batch jobs",
    MAP: "Auto-execution is disabled for map operations",
}
PAUSED_REASON_DEFAULT = "Auto-execution is disabled"

_DECORATOR_RE = re.compile(r"@fused\.udf\(\s*([^)]*)\)")
_ENGINE_KWARG_RE = re.compile(r"\bengine\s*=")
_INSTANCE_KWARG_RE = re.compile(r"\binstance_type\s*=")

# Values of `instance_type` / `engine` that are NOT a batch engine.
_REALTIME_INSTANCE_TYPES = ("realtime",)
_REALTIME_ENGINES = ("realtime", "local")


def confirm_title(kind: str) -> str:
    return CONFIRM_TITLES.get(kind, CONFIRM_TITLE_DEFAULT)


def paused_reason(kind: str) -> str:
    return PAUSED_REASONS.get(kind, PAUSED_REASON_DEFAULT)


def _decorator_names_an_engine(code: str) -> bool:
    """Pass 1: the first `@fused.udf(…)`'s arguments carry `engine=` or
    `instance_type=`. The value is not read."""
    match = _DECORATOR_RE.search(code)
    if match is None:
        return False
    args = match.group(1)
    return bool(_ENGINE_KWARG_RE.search(args) or _INSTANCE_KWARG_RE.search(args))


def _value_text(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant):
        return None if node.value is None else str(node.value)
    try:
        return ast.unparse(node).strip().strip("\"'")
    except Exception:  # noqa: BLE001 — a value that cannot be read says nothing
        return None


def _batch_kwarg(keywords: list[ast.keyword]) -> bool:
    """A keyword list names a batch engine."""
    for kw in keywords:
        if kw.arg is None:
            continue
        value = _value_text(kw.value)
        if value is None:
            continue
        if kw.arg == "instance_type" and value not in _REALTIME_INSTANCE_TYPES:
            return True
        if kw.arg == "engine" and value not in _REALTIME_ENGINES:
            return True
    return False


def _is_fused_attr(node: ast.expr, attrs: tuple[str, ...]) -> bool:
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "fused" and node.attr in attrs)


def _is_fused_call(node: ast.expr, attrs: tuple[str, ...]) -> bool:
    return isinstance(node, ast.Call) and _is_fused_attr(node.func, attrs)


def _udf_names(tree: ast.AST) -> set[str]:
    """Names that hold a UDF: functions decorated `@fused.udf` (with or
    without parentheses) and variables assigned from `fused.load(…)`."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if _is_fused_attr(decorator, ("udf",)) or _is_fused_call(decorator, ("udf",)):
                    names.add(node.name)
        elif isinstance(node, ast.Assign) and _is_fused_call(node.value, ("load",)):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _tree_execution_type(tree: ast.AST) -> str:
    """Pass 2, in the workbench's search order: decorator, map, method."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if _is_fused_call(decorator, ("udf",)) and _batch_kwarg(decorator.keywords):
                    return DECORATOR_BATCH
    names = _udf_names(tree)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "map" and isinstance(node.func.value, ast.Name)
                and node.func.value.id in names):
            return MAP
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not any(kw.arg in ("engine", "instance_type") for kw in node.keywords):
            continue
        direct = isinstance(node.func, ast.Name) and node.func.id in names
        if (direct or _is_fused_attr(node.func, ("run", "submit"))) and _batch_kwarg(node.keywords):
            return INNER_BATCH
    return NONE


def execution_type(code: str) -> str:
    """The kind of run `code` is; anything but "none" is asked about first."""
    if not isinstance(code, str):
        return NONE
    if _decorator_names_an_engine(code):
        return DECORATOR_BATCH
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError):
        return NONE
    return _tree_execution_type(tree)
