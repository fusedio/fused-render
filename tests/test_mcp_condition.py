"""The `mcp` template's condition.py gate (SPEC CT-12, §46 / MC-1).

`mcp` curates a fused-render app's Python entrypoints into MCP tools, so the
gate's question is "is this folder an app?" — a page plus at least one callable
`main`. Four properties are tested directly:

* **Both halves are required.** A folder with `index.html` but no `main()` has
  nothing to curate; a folder of `main()`s with no page is not an app. Either
  half alone is False.
* **A file is never offered.** `mcp` is FOLDER-ONLY like `git`: the manifest it
  writes lives at the app folder's root and covers the whole folder, so the
  folder is the target. The registry says the same thing (`mcp` rides the "/"
  directory key alone) and the gate says it again, so a hand-written
  `?_mode=mcp` on a file gets a no.
* **The listing is bounded, and only paid for by a page.** The gate reads ONE
  directory level, and only after `index.html` is confirmed — so an ordinary
  folder costs a single stat, exactly like its peer gates. Recursion
  (`os.walk`/`glob`) stays fatal for the whole suite, and the folders that never
  reach the listing are asserted not to have listed at all.
* **It fails closed** (CT-12): a mount-backed path, an unavailable mount
  detector, an unreadable path, an unparseable `.py` — every one is False.
"""
import contextlib
import importlib.util
import os
from unittest import mock

import pytest

from _thread_scoped import this_thread_only

CONDITION = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "mcp", "condition.py")

_PAGE = "<h1>mail</h1>\n<script>fused.runPython('mail.py', {op: 'list'})</script>\n"
_DISPATCHER = '"""Local mail app."""\n\n\ndef main(op="list", to=None):\n    return [op, to]\n'


def _load():
    spec = importlib.util.spec_from_file_location("mcp_condition", CONDITION)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main


@contextlib.contextmanager
def _no_recursion():
    """Recursion or globbing inside a gate call is a test failure.

    A single-level listing IS allowed here (see the module docstring) and is
    covered by its own tests; what must never appear is a WALK — the shape that
    wedges a mount and the rule `zarr_aoi/condition.py` documents. Thread-scoped
    for the reason `tests/test_git_condition.py::_no_enumeration` documents:
    another package's background thread globs its own directory on its own
    schedule and would otherwise be blamed on the gate.
    """
    import glob as glob_mod

    def forbidden(*args, **kwargs):
        raise AssertionError("the gate must never walk or glob a directory (CT-12)")

    def guard(target, name):
        real = getattr(target, name)
        return mock.patch.object(target, name, this_thread_only(real, forbidden))

    with guard(os, "walk"), guard(glob_mod, "glob"), guard(glob_mod, "iglob"):
        yield


@pytest.fixture
def gate():
    """The real gate, called with recursive enumeration made fatal."""
    main = _load()

    def call(path):
        with _no_recursion():
            return main(path)

    return call


def _app(tmp_path, name="open-mail", *, page=_PAGE, py=_DISPATCHER, py_name="mail.py"):
    """An app folder: a page plus a dispatcher with a top-level `main`."""
    app = tmp_path / name
    app.mkdir()
    if page is not None:
        (app / "index.html").write_text(page, encoding="utf-8")
    if py is not None:
        (app / py_name).write_text(py, encoding="utf-8")
    return str(app)


# ------------------------------------------------------------------- it detects


def test_an_app_folder_is_offered(gate, tmp_path):
    assert gate(_app(tmp_path)) is True


def test_the_entrypoint_file_may_be_named_anything(gate, tmp_path):
    assert gate(_app(tmp_path, py_name="server.py")) is True


def test_one_qualifying_file_among_several_is_enough(gate, tmp_path):
    app = _app(tmp_path)
    # Neither of these defines a top-level `main`; the dispatcher still does.
    with open(os.path.join(app, "helpers.py"), "w", encoding="utf-8") as fh:
        fh.write("def helper():\n    return 1\n")
    with open(os.path.join(app, "broken.py"), "w", encoding="utf-8") as fh:
        fh.write("def main(:\n")
    assert gate(app) is True


def test_an_async_main_counts(gate, tmp_path):
    assert gate(_app(tmp_path, py="async def main(op=None):\n    return op\n")) is True


# ------------------------------------------------------------------- it refuses


def test_a_page_with_no_python_is_not_offered(gate, tmp_path):
    # Nothing to curate into a tool: an MCP server over this folder would be
    # empty, so the mode is not offered rather than offered-then-empty.
    assert gate(_app(tmp_path, py=None)) is False


def test_python_with_no_page_is_not_offered(gate, tmp_path):
    # A folder of scripts is not an app. `index.html` is what makes the Python
    # an app's entrypoints rather than someone's library.
    assert gate(_app(tmp_path, page=None)) is False


def test_a_module_without_a_top_level_main_is_not_offered(gate, tmp_path):
    py = "class App:\n    def main(self):\n        return 1\n"
    # A `main` METHOD is not an entrypoint: the runner looks the name up in the
    # executed module's namespace, where a method does not appear.
    assert gate(_app(tmp_path, py=py)) is False


def test_a_nested_entrypoint_does_not_count(gate, tmp_path):
    # One level only. A `main` a directory down is not the app's entrypoint, and
    # descending to find one is the walk this gate must never do.
    app = _app(tmp_path, py=None)
    os.makedirs(os.path.join(app, "lib"))
    with open(os.path.join(app, "lib", "mail.py"), "w", encoding="utf-8") as fh:
        fh.write(_DISPATCHER)
    assert gate(app) is False


def test_a_file_target_is_not_offered(gate, tmp_path):
    app = _app(tmp_path)
    assert gate(os.path.join(app, "index.html")) is False
    assert gate(os.path.join(app, "mail.py")) is False


def test_a_missing_path_is_not_offered(gate, tmp_path):
    assert gate(str(tmp_path / "nope")) is False
    assert gate(str(tmp_path / "nope" / "deeper")) is False


def test_an_empty_path_is_not_offered(gate):
    assert gate("") is False


def test_an_unparseable_entrypoint_alone_is_not_offered(gate, tmp_path):
    assert gate(_app(tmp_path, py="def main(:\n")) is False


# ----------------------------------------------------------------- mount refusal


def test_a_mount_backed_path_is_never_offered(gate, tmp_path, monkeypatch):
    app = _app(tmp_path)
    assert gate(app) is True  # the same folder, before it looks mount-backed
    # The env contract the app exports (FUSED_RENDER_MOUNTS_DIR) — how the gate
    # learns the mounts root without importing fused_render (SPEC PY-15).
    monkeypatch.setenv("FUSED_RENDER_MOUNTS_DIR", app)
    assert gate(app) is False


def test_an_unavailable_mount_detector_fails_closed(gate, tmp_path, monkeypatch):
    # "Cannot tell" reads as "refuse": the gate exists to keep reads off a mount,
    # and a guess is not good enough for that.
    import builtins

    app = _app(tmp_path)
    real_import = builtins.__import__

    def deny(name, *args, **kwargs):
        if name == "appenv":
            raise ImportError("no appenv")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny)
    assert gate(app) is False


# ------------------------------------------------------------- bounded, and cheap


def test_a_folder_without_a_page_never_lists(gate, tmp_path, monkeypatch):
    """The listing is the expensive half, and only a page buys it.

    `mcp` rides the universal "/" key, so this gate answers for every directory
    the user opens. Almost none of them hold an `index.html`, and those must
    cost what a peer gate costs: one stat. This pins the ordering — the cheap
    marker probe first, the listing only after it passes — so a later
    refactor cannot quietly make every folder in someone's home directory pay
    for a listing.
    """
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "mail.py").write_text(_DISPATCHER, encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("a folder with no index.html must not be listed")

    monkeypatch.setattr(os, "scandir", this_thread_only(os.scandir, forbidden))
    monkeypatch.setattr(os, "listdir", this_thread_only(os.listdir, forbidden))
    assert gate(str(plain)) is False


def test_the_listing_happens_once(gate, tmp_path, monkeypatch):
    # One directory level, read once — not once per candidate file.
    app = _app(tmp_path)
    calls = []
    real = os.scandir

    def counted(*args, **kwargs):
        calls.append(args[0] if args else None)
        return real(*args, **kwargs)

    monkeypatch.setattr(os, "scandir", counted)
    assert gate(app) is True
    assert len(calls) == 1


def test_a_folder_of_many_python_files_reads_a_bounded_number(gate, tmp_path):
    """A qualifying `main` is looked for in a BOUNDED number of files.

    A directory can hold thousands of `.py` files, and a gate that parsed all of
    them would turn opening one folder into a stall. The cap means a pathological
    folder whose only `main` sits past it answers False — the gate is the UX, and
    `inspect_app.py` (which reads the folder properly, once, on demand) is the
    guarantee (MD-11).
    """
    app = _app(tmp_path, py=None)
    for i in range(60):
        with open(os.path.join(app, f"mod{i:03d}.py"), "w", encoding="utf-8") as fh:
            fh.write("def helper():\n    return 1\n")

    reads = []
    real_open = open

    def counted_open(file, *args, **kwargs):
        if str(file).endswith(".py"):
            reads.append(str(file))
        return real_open(file, *args, **kwargs)

    with mock.patch("builtins.open", counted_open):
        assert gate(app) is False
    assert len(reads) <= 24, f"read {len(reads)} .py files to answer one gate call"
