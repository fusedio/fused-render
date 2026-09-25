"""xlsx/reader.py's fallback for formula cells with no cached value.

reader.py is a runPython target, not a package module — loaded via
importlib and driven directly against tmp_path files, like
test_excel_readonly.py does for its own reader.

Under test:
* A cell with a cached value (openpyxl's normal data_only=True read) is
  shown as-is — nothing here changes that path.
* A formula cell with NO cached value (a workbook never opened in Excel:
  built by openpyxl, or edited by the `excel` template) is evaluated with
  pycel instead of showing up blank.
* A formula pycel can't evaluate (unknown function) falls back to blank
  rather than raising — reader.py must not 500 on one bad cell.
* A genuinely blank cell (no formula at all) stays blank; pycel is never
  invoked for it.
* Pagination (offset/limit, total_rows) is unaffected by any of the above.
* A date-returning formula (DATE/TODAY/…, or a plain offset off a date
  cell) comes back as a date, not pycel's raw day-serial number.
* pycel runs in its own bounded subprocess (not in-process, since building
  its dependency graph is not bounded) — a workbook over the size guard, or
  a worker that hangs past the timeout, degrades to blank instead of
  stalling the (timeout-free) in-process reader.
"""
import importlib.util
import os
import time

import openpyxl
import pytest


def _load_reader():
    path = os.path.join("fused_render", "templates", "xlsx", "reader.py")
    spec = importlib.util.spec_from_file_location("xlsx_reader", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(path, rows, sheet_name="Sheet1"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    for row in rows:
        ws.append(row)
    wb.save(path)


def test_formula_with_no_cached_value_is_evaluated(tmp_path):
    path = tmp_path / "book.xlsx"
    # openpyxl never computes formulas on save, so B2/B3 have no cached
    # value — exactly the shape of a workbook that's never been opened in
    # Excel (an invoice built by a script, or edited by the `excel` template).
    _write(path, [["hours", "amount"], [3, "=A2*50"], [0, "=1/A3"]])

    out = _load_reader().main(file=str(path), sheet="Sheet1", offset=0, limit=10)

    assert out["rows"][0] == {"hours": 3, "amount": 150}
    assert out["rows"][1] == {"hours": 0, "amount": "#DIV/0!"}


def test_unsupported_formula_falls_back_to_blank_not_a_crash(tmp_path):
    path = tmp_path / "book.xlsx"
    _write(path, [["a", "b"], [1, "=NOT_A_REAL_FUNCTION(A2)"]])

    out = _load_reader().main(file=str(path), sheet="Sheet1", offset=0, limit=10)

    assert out["rows"][0] == {"a": 1, "b": None}


def test_genuine_blank_cell_stays_blank(tmp_path):
    path = tmp_path / "book.xlsx"
    _write(path, [["a", "b"], [1, None], [2, None]])

    out = _load_reader().main(file=str(path), sheet="Sheet1", offset=0, limit=10)

    assert out["rows"] == [{"a": 1, "b": None}, {"a": 2, "b": None}]


def test_pagination_unaffected_by_formula_fallback(tmp_path):
    path = tmp_path / "book.xlsx"
    rows = [["n", "double"]] + [[i, f"=A{i + 2}*2"] for i in range(5)]
    _write(path, rows)

    out = _load_reader().main(file=str(path), sheet="Sheet1", offset=1, limit=2)

    assert out["total_rows"] == 5
    assert out["rows"] == [{"n": 1, "double": 2}, {"n": 2, "double": 4}]


def test_cached_value_path_is_untouched(tmp_path, monkeypatch):
    """A workbook Excel DID compute (has a cached <v> alongside the <f>) is
    read as before — the fallback never even looks at it, since it only
    triggers on a missing cache. Proven here by breaking the fallback and
    confirming the cached value still comes through regardless."""
    path = tmp_path / "book.xlsx"
    _write(path, [["a", "b"], [1, "=A2*10"]])
    # openpyxl's writer never emits a cached <v> for a formula cell, so one is
    # spliced into the raw sheet XML by hand — the shape a real Excel save
    # produces (SPEC: reader.py's docstring on data_only=True).
    import zipfile

    with zipfile.ZipFile(path) as zin:
        sheet_xml = zin.read("xl/worksheets/sheet1.xml").decode()
        names = zin.namelist()
        contents = {n: zin.read(n) for n in names}
    sheet_xml = sheet_xml.replace("<f>A2*10</f>", "<f>A2*10</f><v>10</v>", 1)
    contents["xl/worksheets/sheet1.xml"] = sheet_xml.encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for n, data in contents.items():
            zout.writestr(n, data)

    mod = _load_reader()
    monkeypatch.setattr(mod, "_pycel_values", lambda file, sheet, cells: (_ for _ in ()).throw(AssertionError("pycel should not run when a cached value exists")))

    out = mod.main(file=str(path), sheet="Sheet1", offset=0, limit=10)
    assert out["rows"][0] == {"a": 1, "b": 10}


def test_no_pycel_available_degrades_to_blank(tmp_path, monkeypatch):
    """If pycel isn't installed (or its import fails for any reason), the
    reader must still return a page — not crash the whole preview."""
    path = tmp_path / "book.xlsx"
    _write(path, [["a", "b"], [1, "=A2*10"]])

    mod = _load_reader()
    monkeypatch.setattr(mod, "_pycel_values", lambda file, sheet, cells: {})

    out = mod.main(file=str(path), sheet="Sheet1", offset=0, limit=10)
    assert out["rows"][0] == {"a": 1, "b": None}


def test_sheet_name_with_spaces_is_quoted_for_pycel(tmp_path):
    path = tmp_path / "book.xlsx"
    _write(path, [["a", "b"], [3, "=A2*2"]], sheet_name="Timesheet invoice")

    out = _load_reader().main(file=str(path), sheet="Timesheet invoice", offset=0, limit=10)
    assert out["rows"][0] == {"a": 3, "b": 6}


def test_date_formula_renders_as_a_date_not_a_serial(tmp_path):
    path = tmp_path / "book.xlsx"
    _write(path, [["issued", "due", "back"], ["=DATE(2024,3,5)", "=A2+30", "=$A$2-10"]])

    out = _load_reader().main(file=str(path), sheet="Sheet1", offset=0, limit=10)

    assert out["rows"][0] == {
        "issued": "2024-03-05T00:00:00",
        "due": "2024-04-04T00:00:00",   # offset off A2 propagates its dateness
        "back": "2024-02-24T00:00:00",  # same, through an absolute $A$2 ref
    }


def test_plain_number_formula_is_unaffected_by_date_detection(tmp_path):
    path = tmp_path / "book.xlsx"
    _write(path, [["hours", "amount"], [3, "=A2*50"]])

    out = _load_reader().main(file=str(path), sheet="Sheet1", offset=0, limit=10)
    assert out["rows"][0] == {"hours": 3, "amount": 150}


def test_oversized_workbook_skips_pycel_without_spawning(tmp_path, monkeypatch):
    path = tmp_path / "book.xlsx"
    _write(path, [["a", "b"], [1, "=A2*10"]])

    mod = _load_reader()
    monkeypatch.setattr(mod, "_PYCEL_MAX_BYTES", 1)  # this tiny file is "too big"
    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda *a, **k: pytest.fail("should never spawn a worker past the size guard"),
    )

    out = mod.main(file=str(path), sheet="Sheet1", offset=0, limit=10)
    assert out["rows"][0] == {"a": 1, "b": None}


def test_hung_worker_times_out_and_degrades_to_blank(tmp_path):
    path = tmp_path / "book.xlsx"
    _write(path, [["a", "b"], [1, "=A2*10"]])

    slow_worker = tmp_path / "slow_worker.py"
    slow_worker.write_text("import sys, time\nsys.stdin.read()\ntime.sleep(60)\n")

    mod = _load_reader()
    mod._PYCEL_WORKER = str(slow_worker)
    mod._PYCEL_TIMEOUT = 0.5

    started = time.monotonic()
    out = mod.main(file=str(path), sheet="Sheet1", offset=0, limit=10)
    elapsed = time.monotonic() - started

    assert out["rows"][0] == {"a": 1, "b": None}
    assert elapsed < 5  # bounded by _PYCEL_TIMEOUT, not left hanging
