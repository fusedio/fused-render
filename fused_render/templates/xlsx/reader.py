"""Reader backing xlsx/template.html. Returns a JSON-safe page of one sheet.

openpyxl in read_only mode streams rows without loading the whole workbook into
memory, so we iterate once: first row is the header, the rest are data. Only the
requested page is collected; total_rows is the honest data-row count.

Legacy ``.xls`` (BIFF binary) is not readable by openpyxl, so those files go
through xlrd instead — same page shape, so template.html doesn't care which
library produced it.
"""
import datetime
import decimal

import openpyxl


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
    return value


def _read_xls(file: str, sheet: str, offset: int, limit: int) -> dict:
    import xlrd

    wb = xlrd.open_workbook(file)
    try:
        sheets = wb.sheet_names()
        active = sheet if sheet in sheets else (sheets[0] if sheets else "")
        ws = wb.sheet_by_name(active) if active else None

        columns = []
        rows = []
        total_rows = 0
        if ws is not None and ws.nrows > 0:
            header = ws.row_values(0)
            columns = [str(v) if v not in (None, "") else f"col{j}" for j, v in enumerate(header)]
            total_rows = ws.nrows - 1
            for r in range(1 + offset, min(1 + offset + limit, ws.nrows)):
                raw = []
                for c in range(ws.ncols):
                    cell = ws.cell(r, c)
                    v = cell.value
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        v = datetime.datetime(*xlrd.xldate_as_tuple(v, wb.datemode)).isoformat()
                    elif cell.ctype == xlrd.XL_CELL_EMPTY:
                        v = None
                    elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                        v = bool(v)
                    raw.append(v)
                rows.append({columns[j] if j < len(columns) else f"col{j}": _jsonify(v)
                             for j, v in enumerate(raw)})
        return {
            "sheets": sheets,
            "sheet": active,
            "columns": columns,
            "rows": rows,
            "total_rows": total_rows,
        }
    finally:
        wb.release_resources()


def main(file: str, sheet: str = "", offset: int = 0, limit: int = 100) -> dict:
    if file.lower().endswith(".xls"):
        return _read_xls(file, sheet, offset, limit)
    # data_only=True returns the last-computed value of formula cells rather than
    # the formula string — matches what a spreadsheet viewer shows.
    wb = openpyxl.load_workbook(file, read_only=True, data_only=True)
    try:
        sheets = wb.sheetnames
        active = sheet if sheet in sheets else (sheets[0] if sheets else "")
        ws = wb[active] if active else None

        columns = []
        rows = []
        total_rows = 0
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
                    rows.append({columns[j] if j < len(columns) else f"col{j}": _jsonify(v)
                                 for j, v in enumerate(raw)})
        return {
            "sheets": sheets,
            "sheet": active,
            "columns": columns,
            "rows": rows,
            "total_rows": total_rows,
        }
    finally:
        wb.close()
