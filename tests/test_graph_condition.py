"""The folder-level `graph` template's condition.py gate (SPEC CT-12, §32).

The gate runs on EVERY directory the user opens, and the mode it gates is
itself a recursive walk — so two properties matter more than the detection
itself and are tested directly:

* **It never enumerates a directory.** `os.listdir`/`os.scandir`/`glob` are
  replaced with raisers for the whole detection suite, so a listing added later
  fails the tests rather than shipping (the rule `zarr_aoi/condition.py`
  documents; on a world-scale remote store a listing blows past the mount
  timeout).
* **A mount-backed path is False, always.** Not a preference — the graph must
  never walk a mount (MD-11), and this is the half of that guarantee the user
  actually sees (the other half is `markdown/graph.py` refusing the root).
"""
import contextlib
import importlib.util
import os
from unittest import mock

import pytest

from _thread_scoped import this_thread_only

CONDITION = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "graph", "condition.py")


def _load():
    spec = importlib.util.spec_from_file_location("graph_condition", CONDITION)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main


@contextlib.contextmanager
def _no_enumeration():
    """Any directory enumeration inside a gate call is a test failure.

    Patched around the CALL rather than as an autouse fixture: pytest's own
    tmp_path machinery lists directories, so a blanket patch would fail every
    test's setup instead of the thing under test.
    """
    import glob as glob_mod

    def forbidden(*args, **kwargs):
        raise AssertionError("the gate must never enumerate a directory (CT-12)")

    # Thread-scoped: these patches are process-wide, and under the
    # fused-engine job another package's background thread polls its own
    # directory with glob — it would trip a gate assertion about code it never
    # ran. See tests/_thread_scoped.py.
    def guard(target, name):
        real = getattr(target, name)
        return mock.patch.object(target, name, this_thread_only(real, forbidden))

    with guard(os, "listdir"), guard(os, "scandir"), guard(os, "walk"), \
            guard(glob_mod, "glob"), guard(glob_mod, "iglob"):
        yield


@pytest.fixture(scope="module")
def gate():
    """The real gate, called with directory enumeration made fatal."""
    main = _load()

    def call(path):
        with _no_enumeration():
            return main(path)

    return call


@pytest.fixture()
def folder(tmp_path):
    def make(*names):
        target = tmp_path / ("d" + str(len(names)))
        target.mkdir()
        for name in names:
            (target / name).write_text("# note\n", encoding="utf-8")
        return str(target)

    return make


def test_a_folder_with_an_index_note_is_offered_the_graph(gate, folder):
    assert gate(folder("index.md")) is True


def test_the_other_casing_is_probed_too(gate, folder):
    # Only a case-INSENSITIVE filesystem answers one for the other, and the
    # probe is free — so both are asked.
    assert gate(folder("Index.md")) is True


def test_a_repository_readme_is_not_a_notes_vault(gate, folder):
    # The whole point of narrowing the gate: `README.md` is in essentially every
    # code repository, and a link graph over one says nothing.
    assert gate(folder("README.md")) is False
    assert gate(folder("readme.md", "Home.md", "notes.md")) is False


def test_a_folder_of_notes_without_an_index_is_not_offered(gate, folder):
    # Deliberate and one-directional: the cost is a mode that has to be asked
    # for by hand, while the alternative — a content sniff — needs a listing.
    assert gate(folder("Some Note.md", "Another.md")) is False


def test_an_ordinary_folder_is_not_offered(gate, folder):
    assert gate(folder("data.parquet", "script.py")) is False


def test_a_file_path_is_not_offered(gate, tmp_path):
    note = tmp_path / "index.md"
    note.write_text("x\n", encoding="utf-8")
    assert gate(str(note)) is False


def test_a_missing_path_is_not_offered(gate, tmp_path):
    assert gate(str(tmp_path / "nope")) is False


def test_a_mount_backed_folder_is_never_offered(gate, folder, monkeypatch):
    target = folder("index.md")
    assert gate(target) is True  # the same folder, before it looks mount-backed
    # The env contract the app exports (FUSED_RENDER_MOUNTS_DIR), which is how
    # the gate learns the mounts root now — no fused_render import.
    monkeypatch.setenv("FUSED_RENDER_MOUNTS_DIR", target)
    assert gate(target) is False


def test_an_unavailable_mount_detector_fails_closed(gate, folder, monkeypatch):
    # "Cannot tell" must read as "refuse": the gate exists to keep a walk off a
    # mount, and a guess is not good enough for that.
    import builtins
    import sys

    target = folder("index.md")
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "appenv":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    monkeypatch.delitem(sys.modules, "appenv", raising=False)
    assert gate(target) is False


def test_a_probe_error_fails_closed(gate, folder, monkeypatch):
    target = folder("index.md")
    monkeypatch.setattr(os.path, "isfile", lambda p: (_ for _ in ()).throw(OSError("boom")))
    assert gate(target) is False
