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
* The workbook is evaluated ONCE per file version, not per page: later pages,
  and a fresh process (the disk cache), never spawn the worker again; an
  edit to the file does.
* The agent tools — sheets, describe, query, cells — answer from that same
  snapshot; query's SQL is sandboxed (no files, no network) and time-bounded.
"""
import importlib.util
import os
import time

import pytest

# Not every test environment installs the `bundled` extra (test_excel_readonly.py
# notes the same gap for its own reader) — skip the whole file rather than fail
# collection, since there's no meaningful xlsx-formula test without either.
openpyxl = pytest.importorskip("openpyxl")
pytest.importorskip("pycel")


def _load(name):
    path = os.path.join("fused_render", "templates", "xlsx", name)
    spec = importlib.util.spec_from_file_location(name[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _isolated_caches(tmp_path, monkeypatch):
    # Both caches outlive a module load by design (the in-memory one rides on
    # the openpyxl module, the disk one under ~) — so every test starts cold.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delitem(openpyxl.__dict__, "_fused_render_xlsx_snapshots", raising=False)


def _load_reader():
    return _load("reader.py")


def _forget_snapshots():
    openpyxl.__dict__.pop("_fused_render_xlsx_snapshots", None)


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
    monkeypatch.setattr(mod, "_formula_values", lambda *a: pytest.fail("pycel should not run when a cached value exists"))

    out = mod.main(file=str(path), sheet="Sheet1", offset=0, limit=10)
    assert out["rows"][0] == {"a": 1, "b": 10}


def test_no_pycel_available_degrades_to_blank(tmp_path, monkeypatch):
    """If pycel isn't installed (or its import fails for any reason), the
    reader must still return a page — not crash the whole preview."""
    path = tmp_path / "book.xlsx"
    _write(path, [["a", "b"], [1, "=A2*10"]])

    mod = _load_reader()
    monkeypatch.setattr(mod, "_formula_values", lambda *a: {})

    out = mod.main(file=str(path), sheet="Sheet1", offset=0, limit=10)
    assert out["rows"][0] == {"a": 1, "b": None}
    assert out["unevaluated"] == 1  # reported, not silently blank


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


def test_day_count_formula_stays_a_plain_number(tmp_path):
    """`TODAY()-A2` and `DATE(...)-DATE(...)` both start with a name in the
    date-function list, but they return a day COUNT (a "days overdue" /
    duration shape), not a date — the date heuristic must look past the
    prefix match to whether the WHOLE formula is that one call."""
    path = tmp_path / "book.xlsx"
    _write(path, [
        ["due", "elapsed", "span"],
        ["=DATE(2024,1,1)", "=DATE(2024,1,11)-A2", "=DATE(2024,3,5)-DATE(2024,1,1)"],
    ])

    out = _load_reader().main(file=str(path), sheet="Sheet1", offset=0, limit=10)

    assert out["rows"][0]["due"] == "2024-01-01T00:00:00"
    assert out["rows"][0]["elapsed"] == 10
    assert out["rows"][0]["span"] == 64


def test_plain_number_formula_is_unaffected_by_date_detection(tmp_path):
    path = tmp_path / "book.xlsx"
    _write(path, [["hours", "amount"], [3, "=A2*50"]])

    out = _load_reader().main(file=str(path), sheet="Sheet1", offset=0, limit=10)
    assert out["rows"][0] == {"hours": 3, "amount": 150}


def test_oversized_workbook_is_streamed_not_snapshotted(tmp_path, monkeypatch):
    """Past the snapshot cap, the grid still pages (cached values only, one
    streaming pass) — nothing loads the workbook into the server's memory or
    spawns pycel over it — and the analysis tools refuse it by name."""
    path = tmp_path / "book.xlsx"
    _write(path, [["a", "b"], [1, "=A2*10"], [2, "x"], [3, "y"]])

    mod = _load_reader()
    monkeypatch.setattr(mod, "_SNAPSHOT_MAX_BYTES", 1)  # this tiny file is "too big"
    monkeypatch.setattr(mod, "_build", lambda *a: pytest.fail("snapshotted past the cap"))

    out = mod.main(file=str(path), sheet="Sheet1", offset=1, limit=1)
    assert out["total_rows"] == 3 and out["columns"] == ["a", "b"]
    assert out["rows"] == [{"a": 2, "b": "x"}]
    with pytest.raises(mod.TooLarge, match="excel template"):
        mod.describe(str(path))


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


def _no_spawn(mod, monkeypatch):
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *a, **k: pytest.fail("the workbook was evaluated again"))


def test_workbook_is_evaluated_once_not_per_page(tmp_path, monkeypatch):
    path = tmp_path / "book.xlsx"
    rows = [["n", "double"]] + [[i, f"=A{i + 2}*2"] for i in range(300)]
    rows.append([None, "=SUM(B2:B301)"])  # a total on the LAST page reaches every row
    _write(path, rows)

    mod = _load_reader()
    first = mod.main(file=str(path), offset=0, limit=100)
    _no_spawn(mod, monkeypatch)
    last = mod.main(file=str(path), offset=300, limit=100)

    assert first["rows"][1] == {"n": 1, "double": 2}
    assert last["rows"] == [{"n": None, "double": sum(i * 2 for i in range(300))}]


def test_a_fresh_process_reuses_the_disk_cache(tmp_path, monkeypatch):
    path = tmp_path / "book.xlsx"
    _write(path, [["a", "b"], [2, "=A2*10"]])
    _load_reader().main(file=str(path))

    _forget_snapshots()  # what a new process (an MCP call) starts with
    mod = _load_reader()
    _no_spawn(mod, monkeypatch)
    assert mod.main(file=str(path))["rows"] == [{"a": 2, "b": 20}]


def test_editing_the_file_reevaluates_it(tmp_path):
    path = tmp_path / "book.xlsx"
    _write(path, [["a", "b"], [2, "=A2*10"]])
    mod = _load_reader()
    assert mod.main(file=str(path))["rows"] == [{"a": 2, "b": 20}]

    _write(path, [["a", "b"], [3, "=A2*10"]])
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 10**9))  # a distinct mtime, however coarse the fs
    assert mod.main(file=str(path))["rows"] == [{"a": 3, "b": 30}]


def test_non_finite_results_are_excel_num_errors_not_invalid_json():
    import math

    worker = _load("_pycel_worker.py")
    assert worker._plain(math.inf) == "#NUM!"
    assert worker._plain(-math.inf) == "#NUM!"
    assert worker._plain(math.nan) is None
    assert _load_reader()._jsonify(math.inf) == "#NUM!"


def test_repeated_and_blank_headers_stay_addressable(tmp_path):
    path = tmp_path / "book.xlsx"
    _write(path, [["amount", "Amount", None], [1, 2, 3]])

    out = _load_reader().main(file=str(path))
    assert out["columns"] == ["amount", "Amount_2", "col2"]
    assert out["rows"] == [{"amount": 1, "Amount_2": 2, "col2": 3}]


def _sales(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Q1 sales"
    ws.append(["region", "units", "price", "revenue"])
    for i, (region, units, price) in enumerate(
            [("N", 3, 10.5), ("S", 5, 2.5), ("N", 1, 4), ("E", 0, 1)], start=2):
        ws.append([region, units, price, f"=B{i}*C{i}"])
    ws.append(["total", "=SUM(B2:B5)", None, "=1/0"])
    other = wb.create_sheet("Notes")
    other.append(["note"])
    other.append(["hello"])
    wb.save(path)


def test_sheets_lists_tables_with_types(tmp_path):
    path = tmp_path / "sales.xlsx"
    _sales(path)

    out = _load_reader().sheets(str(path))
    q1, notes = out["sheets"]
    assert q1["name"] == "Q1 sales" and q1["rows"] == 5 and q1["unevaluated"] == 0
    assert {c["name"]: c["type"] for c in q1["columns"]} == {
        "region": "string", "units": "int64", "price": "double", "revenue": "double"}
    assert notes == {"name": "Notes", "rows": 1, "columns": [{"name": "note", "type": "string"}],
                     "formulas": 0, "unevaluated": 0}


def test_describe_gives_column_statistics(tmp_path):
    path = tmp_path / "sales.xlsx"
    _sales(path)

    out = _load_reader().describe(str(path), columns="units,revenue,region")
    units, revenue, region = out["columns"]
    assert units == {"column": "units", "type": "int64", "count": 5, "nulls": 0, "distinct": 5,
                     "min": 0, "max": 9, "mean": 3.6, "median": 3.0,
                     "std": pytest.approx(3.577708), "sum": 18}
    # the #DIV/0! total is an error, not a number: NULL in SQL, and counted
    assert revenue["sum"] == 48.0 and revenue["nulls"] == 1 and revenue["errors"] == 1
    assert region["top"][0] == {"value": "N", "count": 2}


def test_describe_names_a_missing_column(tmp_path):
    path = tmp_path / "sales.xlsx"
    _sales(path)
    with pytest.raises(ValueError, match="nope"):
        _load_reader().describe(str(path), columns="nope")


def test_query_runs_sql_over_every_sheet(tmp_path):
    path = tmp_path / "sales.xlsx"
    _sales(path)

    out = _load_reader().main(file=str(path), action="query", sql=(
        'SELECT region, sum(revenue) AS revenue FROM "Q1 sales" '
        "WHERE region <> 'total' GROUP BY 1 ORDER BY 2 DESC"))
    assert out["columns"] == ["region", "revenue"]
    assert out["rows"] == [["N", 35.5], ["S", 12.5], ["E", 0.0]]
    assert out["truncated"] is False

    capped = _load_reader().query(str(path), 'SELECT * FROM "Q1 sales"', limit=2)
    assert len(capped["rows"]) == 2 and capped["truncated"] is True


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_csv('/etc/hosts')",
    "COPY \"Q1 sales\" TO 'leak.csv'",
    "ATTACH 'other.db'",
    "SET enable_external_access = true",
])
def test_query_cannot_touch_files_or_reopen_the_sandbox(tmp_path, sql):
    path = tmp_path / "sales.xlsx"
    _sales(path)
    import duckdb

    with pytest.raises(duckdb.Error):
        _load_reader().query(str(path), sql)


def test_query_is_time_bounded(tmp_path):
    path = tmp_path / "sales.xlsx"
    _sales(path)
    mod = _load_reader()
    mod._QUERY_TIMEOUT = 0.3

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        mod.query(str(path), "SELECT count(*) FROM range(100000) a, range(100000) b WHERE a.range + b.range = 7")
    assert time.monotonic() - started < 5


def test_cells_returns_values_and_formulas(tmp_path):
    path = tmp_path / "sales.xlsx"
    _sales(path)

    out = _load_reader().main(file=str(path), action="cells", ref="'Q1 sales'!B6:D6")
    assert out["sheet"] == "Q1 sales"
    assert out["cells"] == [[
        {"ref": "B6", "value": 9, "formula": "=SUM(B2:B5)"},
        {"ref": "C6", "value": None, "formula": None},
        {"ref": "D6", "value": "#DIV/0!", "formula": "=1/0"},
    ]]


def test_unknown_action_is_refused(tmp_path):
    path = tmp_path / "sales.xlsx"
    _sales(path)
    with pytest.raises(ValueError, match="unknown action"):
        _load_reader().main(file=str(path), action="drop")
