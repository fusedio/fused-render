"""Pure-helper tests for `_fused_share_app.py`, the in-interpreter SDK shim
Share uses for both apps and plain files (share_app.py, share_file.py).

The module imports the real `fused` SDK at load time, which is not installed
in the test environment (by design — the SDK only runs in the shim's own
subprocess). We stub the handful of names the module touches at import time
(`fused`, `fused._global_api.get_api`, `fused._options.options`) so the pure
helper functions — canvas_name, udf_slug, _wrapper_source, _canvas_toml,
_canvas_zip — can be exercised with no network and no real SDK, the same
import-isolation style as test_appfile.py's `isolated_home` fixture uses for
disk state.
"""
from __future__ import annotations

import sys
import types

import pytest


def _install_fake_fused(monkeypatch):
    fused_mod = types.ModuleType("fused")
    fused_mod._env = lambda name: None
    fused_mod.api = types.SimpleNamespace()

    global_api_mod = types.ModuleType("fused._global_api")
    global_api_mod.get_api = lambda: None

    options_mod = types.ModuleType("fused._options")
    options_mod.options = types.SimpleNamespace(
        shared_udf_base_url="https://udf.fused.ai",
        base_web_url="https://www.fused.io",
    )

    monkeypatch.setitem(sys.modules, "fused", fused_mod)
    monkeypatch.setitem(sys.modules, "fused._global_api", global_api_mod)
    monkeypatch.setitem(sys.modules, "fused._options", options_mod)


@pytest.fixture
def shim(monkeypatch):
    _install_fake_fused(monkeypatch)
    # Import fresh each test so a stub swapped in by another test module
    # cannot leak a stale module object in here.
    sys.modules.pop("fused_render._fused_share_app", None)
    import fused_render._fused_share_app as mod
    yield mod
    sys.modules.pop("fused_render._fused_share_app", None)


def test_canvas_name_unchanged_for_an_app_id(shim):
    assert shim.canvas_name("my-cool-app-1a2b3c4d") == "my_cool_app_1a2b3c4d"


def test_udf_slug_unchanged_for_an_app_id(shim):
    assert shim.udf_slug("my-cool-app-1a2b3c4d") == "my_cool_app_1a2b3c4d"


def test_udf_slug_still_prefixes_a_leading_digit(shim):
    assert shim.udf_slug("1-cool-app").startswith("app_")


def test_udf_slug_still_truncates_and_falls_back(shim):
    assert shim.udf_slug("---") == "app"
    assert len(shim.udf_slug("x" * 200)) <= 60


def test_wrapper_source_embeds_the_viewer_token_it_was_handed(shim):
    src = shim._wrapper_source("some_slug", "s3://bucket/key.parquet", "some file",
                                "UDF_DuckDB_Parquet")
    assert '_UDF = "UDF_DuckDB_Parquet"' in src
    assert '_PATH = "s3://bucket/key.parquet"' in src
    assert "fused.load(_UDF)(path=_PATH)" in src


def test_wrapper_source_for_an_app_still_embeds_its_viewer_token(shim):
    src = shim._wrapper_source("my_app", "s3://bucket/app.fused", "My App",
                                "UDF_Fused_App_File")
    assert '_UDF = "UDF_Fused_App_File"' in src


def test_canvas_toml_shape_is_unchanged(shim):
    toml = shim._canvas_toml("my_cool_app", "my_cool_app", "My Cool App")
    assert 'type = "canvas"' in toml
    assert 'udfName = "my_cool_app"' in toml
    assert 'title = "My Cool App"' in toml


def test_canvas_zip_contains_toml_and_wrapper_with_the_given_viewer_token(shim):
    import zipfile
    import io

    data = shim._canvas_zip("my_cool_app", "my_cool_app", "My Cool App",
                             "s3://bucket/app.fused", "UDF_Fused_App_File")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        assert names == {"canvas.toml", "my_cool_app.py"}
        wrapper = zf.read("my_cool_app.py").decode()
        assert '_UDF = "UDF_Fused_App_File"' in wrapper


def test_canvas_zip_for_a_plain_file_uses_the_files_viewer_token(shim):
    import zipfile
    import io

    data = shim._canvas_zip("report_csv_ab12cd", "report_csv_ab12cd", "report.csv",
                             "s3://bucket/report.csv", "UDF_Pandas_CSV")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        wrapper = zf.read("report_csv_ab12cd.py").decode()
        assert '_UDF = "UDF_Pandas_CSV"' in wrapper
        assert '_PATH = "s3://bucket/report.csv"' in wrapper
