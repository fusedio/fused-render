"""A third-party app's index manifest ([tool.fused-render.index] in its own
pyproject.toml) and the propose/confirm registry that backs "app proposes,
user confirms, never silent" (SPEC-index-plugins.md decision #8). Mirrors
background_apps.py's manifest-parsing and autostart-store conventions.
"""
import os

import pytest

from fused_render.index import manifest as manifest_mod
from fused_render.index.manifest import load_manifest


def _write_pyproject(folder, body):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "pyproject.toml"), "w", encoding="utf-8") as fh:
        fh.write(body)


def _write_module(folder, name="indexer.py", body="def register_kind(*, replace=False):\n    pass\n"):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
        fh.write(body)


# -- load_manifest ------------------------------------------------------------


def test_load_manifest_none_without_a_pyproject(tmp_path):
    assert load_manifest(str(tmp_path)) is None


def test_load_manifest_none_on_corrupt_toml(tmp_path):
    _write_pyproject(str(tmp_path), "{not valid toml")
    assert load_manifest(str(tmp_path)) is None


def test_load_manifest_none_without_the_index_table(tmp_path):
    _write_pyproject(str(tmp_path), '[tool.fused-render.app]\nmain = "x.py"\n')
    assert load_manifest(str(tmp_path)) is None


def test_load_manifest_none_when_module_is_missing(tmp_path):
    _write_pyproject(
        str(tmp_path),
        '[tool.fused-render.index]\nmodule = "indexer.py"\nkind = "widgets"\n',
    )
    assert load_manifest(str(tmp_path)) is None


def test_load_manifest_none_when_kind_is_missing(tmp_path):
    _write_module(str(tmp_path))
    _write_pyproject(str(tmp_path), '[tool.fused-render.index]\nmodule = "indexer.py"\n')
    assert load_manifest(str(tmp_path)) is None


def test_load_manifest_rejects_a_module_path_escaping_the_folder(tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("def register_kind(*, replace=False):\n    pass\n", encoding="utf-8")
    folder = tmp_path / "app"
    _write_pyproject(
        str(folder),
        '[tool.fused-render.index]\nmodule = "../outside.py"\nkind = "widgets"\n',
    )
    assert load_manifest(str(folder)) is None


def test_load_manifest_rejects_a_directory_as_the_module(tmp_path):
    folder = tmp_path / "app"
    os.makedirs(str(folder / "notafile.py"), exist_ok=True)
    _write_pyproject(
        str(folder),
        '[tool.fused-render.index]\nmodule = "notafile.py"\nkind = "widgets"\n',
    )
    assert load_manifest(str(folder)) is None


def test_load_manifest_returns_module_and_kind(tmp_path):
    folder = tmp_path / "app"
    _write_module(str(folder))
    _write_pyproject(
        str(folder),
        '[tool.fused-render.index]\nmodule = "indexer.py"\nkind = "widgets"\n',
    )
    m = load_manifest(str(folder))
    assert m is not None
    assert m.folder == os.path.abspath(str(folder))
    assert m.module == os.path.join(os.path.abspath(str(folder)), "indexer.py")
    assert m.kind == "widgets"


# -- propose / confirm registry ------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(manifest_mod, "_store_path",
                        lambda: str(tmp_path / "index_proposals.json"))


def _app_folder(tmp_path, name="myapp", kind="widgets"):
    folder = tmp_path / name
    _write_module(str(folder))
    _write_pyproject(
        str(folder),
        f'[tool.fused-render.index]\nmodule = "indexer.py"\nkind = "{kind}"\n',
    )
    return str(folder)


def test_propose_index_adds_a_valid_manifest_to_pending(tmp_path):
    folder = _app_folder(tmp_path)
    assert manifest_mod.propose_index(folder) is True
    assert os.path.realpath(folder) in manifest_mod.pending_folders()
    assert os.path.realpath(folder) not in manifest_mod.confirmed_folders()


def test_propose_index_false_without_a_valid_manifest(tmp_path):
    folder = tmp_path / "noindex"
    folder.mkdir()
    assert manifest_mod.propose_index(str(folder)) is False
    assert manifest_mod.pending_folders() == []


def test_confirm_index_moves_pending_to_confirmed(tmp_path):
    folder = _app_folder(tmp_path)
    manifest_mod.propose_index(folder)
    assert manifest_mod.confirm_index(folder) is True
    assert os.path.realpath(folder) in manifest_mod.confirmed_folders()
    assert os.path.realpath(folder) not in manifest_mod.pending_folders()


def test_confirm_index_false_when_never_proposed(tmp_path):
    folder = _app_folder(tmp_path)
    assert manifest_mod.confirm_index(folder) is False
    assert manifest_mod.confirmed_folders() == []


def test_refuse_index_drops_a_pending_proposal(tmp_path):
    folder = _app_folder(tmp_path)
    manifest_mod.propose_index(folder)
    manifest_mod.refuse_index(folder)
    assert manifest_mod.pending_folders() == []
    assert manifest_mod.confirmed_folders() == []


def test_refuse_index_revokes_an_already_confirmed_folder(tmp_path):
    folder = _app_folder(tmp_path)
    manifest_mod.propose_index(folder)
    manifest_mod.confirm_index(folder)
    manifest_mod.refuse_index(folder)
    assert manifest_mod.confirmed_folders() == []


# -- import + register (bugbot finding against a7aef9472: confirming a
# proposal only ever rewrote this file's own JSON store — nothing imported
# the folder's module or called `register_kind`, so a confirmed kind never
# appeared in `kinds.registered()` and could never be scanned) ------------


def _registering_app_folder(tmp_path, name="widget_app", kind="_test_widgets"):
    """A folder whose module actually calls `kinds.register` via the
    contract entrypoint (`register_kind`) — unlike `_app_folder`'s bare
    `def register_kind(*, replace=False): pass`, which proves parsing/store
    behavior but never registers anything, this one proves the import half
    works too. Fully qualified `_kinds.register` here, rather than a
    module-scope `from ... import register`, so this fixture's own
    `register_kind` name can never collide with (or be confused for) the
    imported one — see `_reference_shaped_app_folder` below for the
    fixture that DOES use the reference plugin's own import shape, and the
    exact bug that shape used to expose."""
    folder = tmp_path / name
    _write_module(
        str(folder),
        body=(
            "from fused_render.index.kinds import Column, IndexKind\n"
            "\n"
            "def _extract(path, st):\n"
            "    return None\n"
            "\n"
            "KIND = IndexKind(\n"
            f"    name={kind!r}, columns=(Column('name', 'string'),),\n"
            "    extract=_extract, text_column='name',\n"
            ")\n"
            "\n"
            "def register_kind(*, replace=False):\n"
            "    from fused_render.index import kinds as _kinds\n"
            "    _kinds.register(KIND, replace=replace)\n"
        ),
    )
    _write_pyproject(
        str(folder),
        f'[tool.fused-render.index]\nmodule = "indexer.py"\nkind = "{kind}"\n',
    )
    return str(folder)


@pytest.fixture(autouse=True)
def _cleanup_test_kinds():
    """`kinds.py` has no `unregister` (nothing else needs one) — pop this
    suite's own throwaway kind names directly off the module-level registry
    so a passing test here does not leak a registration into another test
    file's `kinds.registered()` (test_index_kinds.py asserts on that list)."""
    from fused_render.index import kinds as kinds_mod
    yield
    for name in list(kinds_mod._REGISTRY):
        if name.startswith("_test_"):
            del kinds_mod._REGISTRY[name]


def test_confirming_a_folder_makes_its_declared_kind_registered(tmp_path):
    from fused_render.index import kinds as kinds_mod

    folder = _registering_app_folder(tmp_path, kind="_test_widgets_confirm")
    manifest_mod.propose_index(folder)
    manifest_mod.confirm_index(folder)
    assert "_test_widgets_confirm" not in kinds_mod.registered()

    manifest_mod.register_confirmed_kinds()

    assert "_test_widgets_confirm" in kinds_mod.registered()


def test_register_confirmed_kinds_is_a_repeatable_noop_once_registered(tmp_path):
    from fused_render.index import kinds as kinds_mod

    folder = _registering_app_folder(tmp_path, kind="_test_widgets_idem")
    manifest_mod.propose_index(folder)
    manifest_mod.confirm_index(folder)

    manifest_mod.register_confirmed_kinds()
    manifest_mod.register_confirmed_kinds()  # must not raise a second time

    assert "_test_widgets_idem" in kinds_mod.registered()


def test_a_pending_but_unconfirmed_folder_is_never_imported(tmp_path):
    from fused_render.index import kinds as kinds_mod

    folder = _registering_app_folder(tmp_path, kind="_test_widgets_pending")
    manifest_mod.propose_index(folder)  # proposed, never confirmed

    manifest_mod.register_confirmed_kinds()

    assert "_test_widgets_pending" not in kinds_mod.registered()


def test_a_confirmed_folder_whose_manifest_went_missing_is_skipped_not_raised(tmp_path):
    """A confirmed folder can outlive its own manifest (the app was deleted,
    or its pyproject.toml edited to drop the table) — `register_confirmed_
    kinds` must degrade to "nothing to register" for it, never raise and
    take every OTHER confirmed folder's registration down with it."""
    folder = _registering_app_folder(tmp_path, kind="_test_widgets_gone")
    manifest_mod.propose_index(folder)
    manifest_mod.confirm_index(folder)
    os.remove(os.path.join(folder, "pyproject.toml"))

    manifest_mod.register_confirmed_kinds()  # must not raise


def _reference_shaped_app_folder(tmp_path, name="ref_widget_app", kind="_test_widgets_ref"):
    """A folder shaped exactly like the shipped reference plugin
    (`examples/notes_indexer/indexer.py`): `from fused_render.index.kinds
    import Column, IndexKind, register` at module scope, with its own
    registration entrypoint under a DIFFERENT name
    (`register_kind`) — unlike `_registering_app_folder`'s `def
    register(replace=False):`, which is defined AFTER the `import
    register` line and so silently REBINDS `register` in the module's own
    namespace to the local function, masking the exact bug a real
    reference-shaped plugin hits: `getattr(module, "register", None)`
    would return the imported `kinds.register` itself, called as
    `fn(replace=True)` -> `TypeError: register() missing 1 required
    positional argument: 'kind'`, swallowed by `_import_and_register`'s
    broad `except Exception`."""
    folder = tmp_path / name
    _write_module(
        str(folder),
        body=(
            "from fused_render.index.kinds import Column, IndexKind, register\n"
            "\n"
            "def _extract(path, st):\n"
            "    return None\n"
            "\n"
            "KIND = IndexKind(\n"
            f"    name={kind!r}, columns=(Column('name', 'string'),),\n"
            "    extract=_extract, text_column='name',\n"
            ")\n"
            "\n"
            "def register_kind(*, replace=False):\n"
            "    register(KIND, replace=replace)\n"
        ),
    )
    _write_pyproject(
        str(folder),
        f'[tool.fused-render.index]\nmodule = "indexer.py"\nkind = "{kind}"\n',
    )
    return str(folder)


def test_a_reference_shaped_plugin_module_actually_registers(tmp_path):
    """The entry-point contract is the fixed name `register_kind` (never a
    bare `register`, which every plugin following the reference example's
    own `from fused_render.index.kinds import ... register` shadows in
    reverse — the IMPORTED name, not a local override). A module that
    matches the reference plugin's real shape must still end up in
    `kinds.registered()`."""
    from fused_render.index import kinds as kinds_mod

    folder = _reference_shaped_app_folder(tmp_path)
    manifest_mod.propose_index(folder)
    manifest_mod.confirm_index(folder)
    assert "_test_widgets_ref" not in kinds_mod.registered()

    manifest_mod.register_confirmed_kinds()

    assert "_test_widgets_ref" in kinds_mod.registered()


def test_reproposing_an_already_confirmed_folder_is_a_silent_no_op(tmp_path):
    """Confirmation is sticky: a folder the user already approved does not
    fall back to "pending" (and thus reprompt) just because the app called
    propose again — that would be exactly the silent-reprompt-fatigue this
    decision exists to avoid, and it would also make "confirmed" not really
    mean confirmed."""
    folder = _app_folder(tmp_path)
    manifest_mod.propose_index(folder)
    manifest_mod.confirm_index(folder)
    assert manifest_mod.propose_index(folder) is True
    assert os.path.realpath(folder) in manifest_mod.confirmed_folders()
    assert os.path.realpath(folder) not in manifest_mod.pending_folders()
