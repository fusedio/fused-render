"""Bounded formula evaluation for xlsx/reader.py.

Building pycel's dependency graph over a workbook is NOT bounded — a large or
heavily cross-referenced file can take a long time or a lot of memory — and
reader.py runs IN-PROCESS in the server (D72, `executor.INPROCESS_HELPERS`).
So this one unbounded step runs here, as a short-lived subprocess reader.py
spawns with a hard timeout. Standalone script: stdlib + the `bundled` extra
(pycel, openpyxl) only, and no `fused_render` import (SPEC PY-15).

Evaluation only. Which results are dates, and everything else about how a
value is shown, is reader.py's call: it already holds each cell's formula and
number format from its own read of the workbook.

Protocol — stdin, one JSON object:
    {"file": str, "cells": [[sheet, row, col], ...]}      (row, col 1-based)
stdout, one JSON array:
    [[sheet, row, col, value], ...]
covering only the cells pycel could evaluate; one it can't (an unsupported
function, a malformed formula) is omitted, and reader.py reports it as
unevaluated. The output is strict JSON: a non-finite result is Excel's
`#NUM!`, never a bare `Infinity` token that would fail the whole parse.
"""
import json
import logging
import math
import re
import sys

# pycel logs a full traceback for every formula it can't evaluate — noise
# here, since an omitted cell already means "could not evaluate".
logging.getLogger("pycel").setLevel(logging.CRITICAL)

_SHEET_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_sheet(name):
    if _SHEET_SAFE.match(name):
        return name
    return "'" + name.replace("'", "''") + "'"


def _plain(value):
    """pycel's result as a JSON scalar."""
    if hasattr(value, "item") and not isinstance(value, (str, bool)):
        value = value.item()  # numpy scalar (SUM, AVERAGE, … return these)
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if math.isinf(value):
            return "#NUM!"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)  # an ExcelCmp error object or similar: its Excel text


def main():
    req = json.load(sys.stdin)

    from openpyxl.utils import get_column_letter
    from pycel import ExcelCompiler

    xl = ExcelCompiler(filename=req["file"])
    out = []
    for sheet, row, col in req["cells"]:
        try:
            value = _plain(xl.evaluate(f"{_quote_sheet(sheet)}!{get_column_letter(col)}{row}"))
        except Exception:
            continue
        out.append([sheet, row, col, value])
    sys.stdout.write(json.dumps(out, allow_nan=False))


if __name__ == "__main__":
    main()
