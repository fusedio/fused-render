"""Bounded worker for xlsx/reader.py's formula fallback.

Building pycel's dependency graph over a workbook is NOT bounded — a large
or heavily cross-referenced file can take a long time or a lot of memory to
compile — but xlsx/reader.py is only allowed to run IN-PROCESS at all
because every other thing it does is a fast, bounded local-file read (D72,
`executor.INPROCESS_HELPERS`). So the one unbounded step is pushed out into
its own short-lived subprocess, spawned by reader.py with a hard timeout —
the same shape executor.py itself uses for arbitrary (unbounded) user code,
just scoped to pycel instead of a whole script. This file is a standalone
script, not a package module: it must run under `sys.executable` with
nothing but stdlib + the `bundled` extra (pycel, openpyxl) on its path, and
it deliberately does not import `fused_render` (SPEC PY-15 — a template's
own code stays decoupled from the app package; see fused_render/_child.py's
docstring for the fuller rationale).

Protocol: one JSON object on stdin —
    {"file": str, "sheet": str, "cells": [[row, col0], ...]}
(row is 1-based, col0 is 0-based, matching openpyxl/get_column_letter). One
JSON array on stdout — [[row, col0, value], ...], covering only the cells
pycel could evaluate; a cell it can't (unsupported function, malformed
formula) is simply omitted, so the caller's existing "leave it blank"
fallback covers that case too.
"""
import json
import logging
import re
import sys

from openpyxl.styles.numbers import is_date_format

# pycel logs a full traceback (via `logging`, not a raised exception) for
# every formula it can't evaluate — noise here, since the caller already
# treats a missing result as "leave this cell blank".
logging.getLogger("pycel").setLevel(logging.CRITICAL)

_SHEET_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Functions whose result is a day count Excel always displays as a date, even
# on a cell whose own number_format was never set to one — the common case
# for a workbook no one has opened in Excel (openpyxl-authored, or edited by
# the `excel` template), which is exactly what this fallback exists for.
_DATE_FUNCS = ("DATE(", "TODAY(", "NOW(", "EDATE(", "EOMONTH(", "WORKDAY(", "WORKDAY.INTL(")
# The other everyday shape: "due date = issue date + 30", a plain offset off
# ONE same-sheet cell reference. Whether that result is a date depends on
# whether the referenced cell holds one — checked against ITS number_format
# below — since nothing here type-checks a formula tree in general (a `SUM`
# over a date range, say, is deliberately left showing a plain number).
_OFFSET_RE = re.compile(
    r"^=?\s*(?:(\$?[A-Za-z]{1,3}\$?[0-9]+)\s*[+-]\s*[0-9]+|[0-9]+\s*\+\s*(\$?[A-Za-z]{1,3}\$?[0-9]+))\s*$"
)


def _quote_sheet(name):
    if _SHEET_SAFE.match(name):
        return name
    return "'" + name.replace("'", "''") + "'"


def _looks_like_date_formula(formula):
    """True only when the WHOLE formula is one date-function call.

    `TODAY()-A2` and `DATE(...)-DATE(...)` also start with a name in
    `_DATE_FUNCS`, but they return a day COUNT (a "days overdue" / duration
    shape), not a date — so a `.startswith()` check alone misreads them.
    Requiring nothing trail the matching close paren rules those out.
    """
    body = formula.lstrip("=").strip()
    upper = body.upper()
    for name in _DATE_FUNCS:
        if not upper.startswith(name):
            continue
        depth = 0
        for i in range(len(name) - 1, len(body)):
            if body[i] == "(":
                depth += 1
            elif body[i] == ")":
                depth -= 1
                if depth == 0:
                    return body[i + 1:].strip() == ""
        return False  # unbalanced parens — not a date we can trust
    return False


def _cell_is_date(ws, ref):
    """Excel would show `ref` as a date: either its OWN number_format says so
    (the file was touched by Excel at some point), or it holds one of the
    _DATE_FUNCS formulas with no such format yet — the exact gap this whole
    fallback exists for, so an offset off of one needs the same check rather
    than just its number_format (which is "General" for a bare formula
    cell)."""
    cell = ws[ref]
    if is_date_format(cell.number_format):
        return True
    return isinstance(cell.value, str) and _looks_like_date_formula(cell.value)


def _offset_source_ref(formula):
    """The one cell reference in a "ref ± N" / "N + ref" formula, or None."""
    m = _OFFSET_RE.match(formula.strip())
    if not m:
        return None
    return (m.group(1) or m.group(2)).upper().replace("$", "")


def _jsonify(value):
    # pycel returns numpy scalars from some functions (SUM, AVERAGE, ...);
    # unwrap them to the plain Python value json.dumps already handles.
    if hasattr(value, "item") and not isinstance(value, (str, bool)):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, float) and value != value:  # NaN has no JSON literal
        return None
    return value


def main():
    req = json.load(sys.stdin)
    file, sheet, cells = req["file"], req["sheet"], req["cells"]

    import openpyxl
    from openpyxl.utils import get_column_letter
    from openpyxl.utils.datetime import from_excel
    from pycel import ExcelCompiler

    # A second, plain openpyxl open just for metadata (formula text, cell
    # number_format, the workbook's date epoch) — cheap next to the pycel
    # compile below, and the only way to get at styles pycel doesn't expose.
    wb = openpyxl.load_workbook(file, read_only=True, data_only=False)
    ws = wb[sheet]
    epoch = wb.epoch

    xl = ExcelCompiler(filename=file)
    qsheet = _quote_sheet(sheet)
    out = []
    for row, col0 in cells:
        col_letter = get_column_letter(col0 + 1)
        addr = f"{qsheet}!{col_letter}{row}"
        try:
            v = xl.evaluate(addr)
        except Exception:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            ref = f"{col_letter}{row}"
            is_date = _cell_is_date(ws, ref)
            if not is_date:
                source = _offset_source_ref(str(ws[ref].value or ""))
                is_date = source is not None and _cell_is_date(ws, source)
            if is_date:
                v = from_excel(v, epoch).isoformat()
        out.append([row, col0, _jsonify(v)])
    wb.close()
    print(json.dumps(out))


if __name__ == "__main__":
    main()
