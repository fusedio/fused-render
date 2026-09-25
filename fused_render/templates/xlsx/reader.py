"""Reader backing xlsx/template.html. Returns a JSON-safe page of one sheet.

openpyxl in read_only mode streams rows without loading the whole workbook into
memory, so we iterate once: first row is the header, the rest are data. Only the
requested page is collected; total_rows is the honest data-row count.
"""
import datetime
import decimal
import json
import os
import subprocess
import sys

import openpyxl

# The worker that actually runs pycel, as its own subprocess with a timeout
# (see _pycel_values below for why this can't just be a function call here).
_PYCEL_WORKER = os.path.join(os.path.dirname(__file__), "_pycel_worker.py")
_PYCEL_TIMEOUT = 8.0
# pycel's cost tracks formula complexity, not file size, but file size is the
# only thing worth checking before paying a subprocess spawn: past this, an
# 8s timeout is more likely to fire than not, and it would fire on every page
# of every sheet. Formula cells just stay on their text fallback instead.
_PYCEL_MAX_BYTES = 25 * 1024 * 1024


def _jsonify(value):
    """Coerce an openpyxl cell value into something json.dumps can encode."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    # pycel returns numpy scalars from some functions (SUM, AVERAGE, ...);
    # unwrap them to the plain Python value json.dumps already handles.
    if hasattr(value, "item") and not isinstance(value, (str, bool)):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    return value


def _pycel_values(file, sheet, cells):
    """Evaluate the given (row, col0) cells with pycel, in a bounded subprocess.

    Building pycel's dependency graph over a workbook is NOT bounded — a
    large or heavily cross-referenced file can take a long time or a lot of
    memory — but this reader is only allowed to run IN-PROCESS at all
    because everything else it does is a fast, bounded local-file read (D72,
    executor.INPROCESS_HELPERS). So the pycel step runs in its own
    short-lived subprocess (`_pycel_worker.py`) with a hard timeout, the same
    shape executor.py itself uses for arbitrary (unbounded) user code.

    `cells` are 1-based sheet rows paired with 0-based column indices.
    Returns a {(row, col0): value} map; a cell that times out or that pycel
    can't evaluate (an unsupported function, a malformed formula) is simply
    absent, and the caller keeps showing that cell's formula text instead of
    guessing at a result.
    """
    if not cells:
        return {}
    try:
        if os.path.getsize(file) > _PYCEL_MAX_BYTES:
            return {}
    except OSError:
        return {}
    request = json.dumps({"file": file, "sheet": sheet, "cells": [list(c) for c in cells]})
    try:
        proc = subprocess.run(
            [sys.executable, _PYCEL_WORKER],
            input=request,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PYCEL_TIMEOUT,
            close_fds=False,  # see executor.py's own subprocess call: avoids a
            # fork() + pthread_atfork crash when pyproj/PROJ is resident
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {}
    try:
        pairs = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return {}  # the worker crashed or produced no output — degrade quietly
    return {(row, col0): v for row, col0, v in pairs}


def main(file: str, sheet: str = "", offset: int = 0, limit: int = 100) -> dict:
    # data_only=True returns the last-computed value of formula cells rather than
    # the formula string — matches what a spreadsheet viewer shows. A workbook
    # that has never been opened in Excel (built by openpyxl, or edited by the
    # `excel` template) has no such cached value, only the formula text — those
    # gaps get filled in below with pycel, scoped to just this page.
    wb = openpyxl.load_workbook(file, read_only=True, data_only=True)
    try:
        sheets = wb.sheetnames
        active = sheet if sheet in sheets else (sheets[0] if sheets else "")
        ws = wb[active] if active else None

        columns = []
        rows = []
        total_rows = 0
        blanks = []  # (data_idx relative to `rows`, col_idx) with no cached value
        if ws is not None:
            for i, raw in enumerate(ws.iter_rows(values_only=True)):
                if i == 0:
                    # First row is the header; blank cells get positional names
                    # so every column is addressable.
                    columns = [
                        str(v) if v is not None else f"col{j}" for j, v in enumerate(raw)
                    ]
                    continue
                data_idx = i - 1  # 0-based index among data rows
                total_rows += 1
                if offset <= data_idx < offset + limit:
                    page_idx = data_idx - offset
                    row_out = {}
                    for j, v in enumerate(raw):
                        col = columns[j] if j < len(columns) else f"col{j}"
                        if v is None:
                            blanks.append((page_idx, i + 1, j, col))  # sheet row is i+1 (1-based)
                        row_out[col] = _jsonify(v)
                    rows.append(row_out)
    finally:
        wb.close()

    if blanks and ws is not None:
        # Second pass, read_only + data_only=False: cheap (same page range),
        # and it's the only way to tell "genuinely blank" apart from "formula
        # with no cached result" — openpyxl can't expose both from one load.
        want = {(row, col) for _, row, col, _ in blanks}
        min_row, max_row = min(r for r, _ in want), max(r for r, _ in want)
        formula_cells = set()
        wb2 = openpyxl.load_workbook(file, read_only=True, data_only=False)
        try:
            ws2 = wb2[active]
            for i, raw in enumerate(ws2.iter_rows(values_only=True)):
                if i == 0:
                    continue
                row = i + 1
                if row < min_row:
                    continue
                if row > max_row:
                    break
                for j, v in enumerate(raw):
                    if (row, j) in want and isinstance(v, str) and v.startswith("="):
                        formula_cells.add((row, j))
        finally:
            wb2.close()

        if formula_cells:
            computed = _pycel_values(file, active, formula_cells)
            for page_idx, row, col, colname in blanks:
                v = computed.get((row, col))
                if v is not None:
                    rows[page_idx][colname] = _jsonify(v)

    return {
        "sheets": sheets,
        "sheet": active,
        "columns": columns,
        "rows": rows,
        "total_rows": total_rows,
    }
