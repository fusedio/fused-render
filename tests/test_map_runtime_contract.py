"""Packaging and process contracts for the built-in Map Viewer runtime."""
from __future__ import annotations

import builtins
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
MAP = ROOT / "fused_render" / "templates" / "map"


def _load(name: str):
    if str(MAP) not in sys.path:
        sys.path.insert(0, str(MAP))
    spec = importlib.util.spec_from_file_location(name, MAP / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_clipboard_quotes_are_removed_before_path_resolution():
    discover = _load("discover")
    map_render = _load("map_render")
    path = r"C:\Data\scene.tif"

    for quoted in (f'"{path}"', f"'{path}'", f"“{path}”", f"‘{path}’"):
        assert discover.clean_path(quoted) == path
        assert map_render._clean_target(quoted) == path


def test_remote_urls_are_never_environment_expanded(monkeypatch):
    discover = _load("discover")
    map_render = _load("map_render")
    monkeypatch.setenv("MAP_OBJECT", "wrong-object")

    for remote, expected in (
        (
            "https://example.test/$MAP_OBJECT/%MAP_OBJECT%/scene.tif",
            "https://example.test/$MAP_OBJECT/%MAP_OBJECT%/scene.tif",
        ),
        (
            "S3://Bucket/$MAP_OBJECT/%MAP_OBJECT%/Scene.gpkg",
            "s3://Bucket/$MAP_OBJECT/%MAP_OBJECT%/Scene.gpkg",
        ),
        (
            "/VSICURL/HTTPS://example.test/$MAP_OBJECT/Scene.fgb",
            "/vsicurl/https://example.test/$MAP_OBJECT/Scene.fgb",
        ),
    ):
        assert discover.clean_path(f'"{remote}"') == expected
        assert map_render._clean_target(f'"{remote}"') == expected


def test_map_render_routes_uppercase_url_without_local_path_conversion(
    tmp_path, monkeypatch
):
    map_render = _load("map_render")
    observed = {}
    monkeypatch.setattr(map_render, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(map_render, "ARTIFACT_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(map_render, "_ensure_service", lambda: {"port": 1})
    monkeypatch.setattr(
        map_render,
        "_post",
        lambda _state, _path, request: observed.update(request)
        or {"status": "ok"},
    )

    result = map_render.main(target="HTTP://Example.test/Data/Scene.TIF")

    assert result == {"status": "ok"}
    assert observed["target"] == "http://Example.test/Data/Scene.TIF"


def test_daemon_bootstraps_sibling_modules_without_launcher_sys_path(
    monkeypatch,
):
    template_dir = str(MAP.resolve())
    monkeypatch.setattr(
        sys,
        "path",
        [
            value
            for value in sys.path
            if str(Path(value or ".").resolve()) != template_dir
        ],
    )
    for name in ("worker", "raster_engine", "vector_engine"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    daemon = _load("daemon")

    assert Path(daemon.worker.__file__).resolve().parent == MAP.resolve()
    assert daemon.RasterEngine.__module__ == "raster_engine"
    assert daemon.VectorEngine.__module__ == "vector_engine"


def test_map_runtime_dependencies_stay_out_of_project_and_platform_packaging():
    """The Map Viewer's runtime is declared in ONE place, and it is not the app.

    Two halves, and only one of them changed with D276. The half that did not:
    the abandoned mapbox-vector-tile/xlrd/pyclipper stack must stay out of both
    `[bundled]` and the macOS force-list, and `map_render.py` must launch the
    plain interpreter it was handed rather than reaching for `uv run` — the
    template does not get to invent its own environment plumbing beside the
    engine's.

    The half that did: `map/pyproject.toml` used to be asserted ABSENT, because
    the geo stack was in `[bundled]` and a folder manifest would have bought a
    download for packages the app already had. D276 took that stack out of the
    extra, so the same reasoning now demands the opposite — the manifest is
    where those dependencies live, and its absence would leave every map render
    importing rasterio out of an interpreter that has none. Inverted rather than
    deleted: this line is the one that notices if the manifest is ever dropped
    without the extra being restored.
    """
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    bundled = project["project"]["optional-dependencies"]["bundled"]

    assert not any(item.startswith("mapbox-vector-tile") for item in bundled)
    assert not any(item.startswith("xlrd") for item in bundled)

    launcher = (MAP / "map_render.py").read_text(encoding="utf-8")
    assert "[sys.executable," in launcher
    assert "CREATE_NO_WINDOW" in launcher
    assert "uv run" not in launcher
    assert "FUSED_RENDER_UV" not in launcher

    manifest = MAP / "pyproject.toml"
    assert manifest.exists(), (
        "fused_render/templates/map/pyproject.toml is gone. The geo stack left "
        "`[bundled]` in D276, so this folder's manifest is the only place "
        "geopandas/rasterio/rio-tiler/matplotlib are declared — without it every "
        "map render fails at import on a packaged app (SPEC PY-16)."
    )
    assert (MAP / "uv.lock").exists(), (
        "map declares an environment but ships no uv.lock, so a released build "
        "would resolve it against PyPI on first render (SPEC PY-16)"
    )
    declared = tomllib.loads(manifest.read_text(encoding="utf-8"))
    for package in ("mapbox-vector-tile", "xlrd", "pyclipper"):
        assert not any(
            item.startswith(package)
            for item in declared["project"]["dependencies"]
        ), f"{package} is abandoned; it must not come back via the map manifest"

    setup = (ROOT / "scripts" / "setup_py2app.py").read_text(encoding="utf-8")
    for package in ("mapbox_vector_tile", "xlrd", "pyclipper"):
        assert f'"{package}"' not in setup


def test_a_map_target_importing_what_this_venv_lacks_is_told_where_it_ran(tmp_path):
    """The D276 capability regression, made legible (C1).

    A Python map target is exec'd IN THIS TEMPLATE'S PROCESS, because the
    descriptor is built from the live object it returns. That process used to be
    the app interpreter with all of `[bundled]`; it is now map's own environment.
    So a target doing `import duckdb` fails — and the bare `No module named
    'duckdb'` sends the reader to their own folder's pyproject.toml, which is not
    consulted for this call and cannot fix it.

    Asserted on the MESSAGE rather than the type, because the type is right
    already and the message is the entire defect.
    """
    target = tmp_path / "layer.py"
    target.write_text(
        "import a_module_that_does_not_exist\n\ndef main():\n    return None\n",
        encoding="utf-8",
    )
    worker = _load("worker")
    out = worker.main({
        "target": str(target),
        "artifact_dir": str(tmp_path),
        "artifact_id": "t1",
    })

    assert out["status"] == "error"
    message = out["message"]
    assert "a_module_that_does_not_exist" in message
    assert "map/pyproject.toml" in message
    # The correction that stops the reader looking in the wrong place.
    assert "NOT the app's interpreter" in message
    assert "will not change that" in message


def _manifest_dependency_names() -> list[str]:
    declared = tomllib.loads(
        (MAP / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    return [d.split(";")[0].split("[")[0].split(">")[0].split("=")[0].split("<")[0].strip()
            for d in declared]


def _hide_tomllib(monkeypatch):
    """Make `import tomllib` raise, as it does on Python 3.10 and older."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "tomllib":
            raise ImportError("No module named 'tomllib'")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "tomllib", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_the_map_target_message_names_every_package_the_venv_actually_has():
    """The message tells the user to "rewrite the target using the packages
    above", so the list had better be the real one (D177).

    Hand-written, it listed six of thirteen and omitted `duckdb` and `requests`
    — the two put in the manifest specifically so user targets could import
    them, i.e. the two most likely to save the reader. It is derived from the
    manifest now, and this is the check that keeps it derived: a future entry
    added to `map/pyproject.toml` must appear here without anyone remembering.
    """
    names = _manifest_dependency_names()
    assert "duckdb" in names and "requests" in names, (
        "map/pyproject.toml no longer declares the two packages added for "
        "user-supplied targets (D276); if that was deliberate, update this test "
        "and worker.py's docstring together"
    )

    worker = _load("worker")
    message = worker._missing_module_help(
        ModuleNotFoundError("No module named 'x'", name="x"))
    missing = [n for n in names if n not in message]
    assert not missing, (
        f"{missing} are in map/pyproject.toml but absent from the help text a "
        "user gets when their map target fails to import — the message points at "
        "'the packages above' and would be hiding exactly the ones that could "
        "fix their script"
    )


def test_the_package_list_is_still_derived_on_a_python_without_tomllib(monkeypatch):
    """The 3.10 hole this test exists to close.

    `tomllib` is stdlib only from 3.11, and the derivation used to sit inside a
    bare `except Exception: return []`. On `pip install fused-render` under 3.10
    that swallowed the ImportError and the help text named NO packages while
    still telling the reader to use "the packages above" — the exact D177
    failure the derivation was introduced to prevent, only silent. Simulated
    rather than version-gated so it is checked on every interpreter.
    """
    worker = _load("worker")
    _hide_tomllib(monkeypatch)

    names = _manifest_dependency_names()
    assert set(names) <= set(worker._declared_packages())

    message = worker._missing_module_help(
        ModuleNotFoundError("No module named 'x'", name="x"))
    assert [n for n in names if n not in message] == []


def test_an_unreadable_manifest_says_so_instead_of_enumerating_nothing(monkeypatch):
    """The degraded path must be loud, not empty.

    A diagnostic must not raise, so a manifest that cannot be read at all still
    yields no list — but the message then has to admit that, rather than reading
    as if the environment simply contains nothing.
    """
    worker = _load("worker")
    monkeypatch.setattr(worker, "_declared_packages", lambda: [])

    message = worker._missing_module_help(
        ModuleNotFoundError("No module named 'x'", name="x"))
    assert "could not read" in message
    assert "map/pyproject.toml" in message


def test_browsable_vector_formats_are_supported_by_every_loading_path():
    discover = _load("discover")
    classify = _load("geo_classify")
    map_render = _load("map_render")
    vector_engine = _load("vector_engine")

    browsable = set(discover.VECTOR)
    assert browsable <= set(map_render.VECTOR_SUFFIXES)
    assert browsable <= set(vector_engine.VECTOR_SUFFIXES)
    assert browsable <= set(classify.VECTOR_EXT)


def test_optional_runtime_lists_only_missing_distributions(monkeypatch):
    optional_runtime = _load("optional_runtime")
    available = {"rasterio"}
    monkeypatch.setattr(
        optional_runtime,
        "_is_available",
        lambda module: module in available,
    )

    message = optional_runtime.require(
        "Raster layers",
        {
            "rasterio": "rasterio",
            "rio_tiler": "rio-tiler",
            "some_rio_tiler_submodule": "rio-tiler",
        },
    )

    assert "not installed: rio-tiler" in message
    assert message.endswith("uv pip install rio-tiler")
    assert message.count("rio-tiler") == 2
    # D276 makes this message reachable on a PACKAGED app for the first time
    # (the geo stack moved from `[bundled]` into map/pyproject.toml), and a DMG
    # user cannot pip install anything. So the manual command may no longer be
    # the ONLY thing offered — the two causes a packaged user can actually act
    # on come first. Pinned, because "just tell them to pip install" is the
    # shape this keeps regressing to (D176, and pdf_studio's boot panel).
    assert "on first render" in message
    assert "Preferences" in message


def test_large_vector_reports_install_command_before_registering_tiles(
    monkeypatch,
):
    vector_engine = _load("vector_engine")
    monkeypatch.setattr(
        vector_engine,
        "_vector_dependency_error",
        lambda: (
            "Optional support for streamed vector layers requires Python "
            "packages that are not installed: pyarrow.\n"
            "uv pip install pyarrow"
        ),
    )
    engine = vector_engine.VectorEngine(
        base_url="http://127.0.0.1:1234",
        token="test-token",
        locator=lambda source, _target: source,
    )

    descriptor = engine.try_describe(
        {
            "target": "C:/data/large.gpkg",
            "artifact_id": "vector-1",
        }
    )

    assert descriptor["status"] == "error"
    assert "uv pip install pyarrow" in descriptor["message"]
    assert engine.sources == {}


def test_raster_reports_install_command_before_opening_source(
    monkeypatch,
    tmp_path,
):
    raster_engine = _load("raster_engine")
    source = tmp_path / "large.tif"
    source.write_bytes(b"not opened")
    monkeypatch.setattr(
        raster_engine,
        "_raster_dependency_error",
        lambda: (
            "Optional support for raster layers requires Python packages "
            "that are not installed: rio-tiler.\n"
            "uv pip install rio-tiler"
        ),
    )
    engine = raster_engine.RasterEngine(
        cache_dir=str(tmp_path / "cache"),
        base_url="http://127.0.0.1:1234",
        token="test-token",
    )

    descriptor = engine.try_describe(
        {
            "target": str(source),
            "artifact_id": "raster-1",
        }
    )

    assert descriptor["status"] == "error"
    assert "uv pip install rio-tiler" in descriptor["message"]
    assert engine.sources == {}


def test_legacy_excel_reports_xlrd_install_command(monkeypatch):
    classify = _load("geo_classify")
    monkeypatch.setattr(
        classify,
        "require",
        lambda _feature, _requirements: (
            "Optional support for legacy Excel layers requires Python "
            "packages that are not installed: pandas, xlrd.\n"
            "uv pip install pandas xlrd"
        ),
    )

    with pytest.raises(RuntimeError, match="uv pip install pandas xlrd"):
        classify._from_table("legacy.xls", "", "excel-1", {})


def test_small_vector_can_use_geojson_fallback_on_an_old_runtime(
    monkeypatch,
    tmp_path,
):
    vector_engine = _load("vector_engine")
    source = tmp_path / "small.gpkg"
    source.write_bytes(b"small")
    monkeypatch.setattr(
        vector_engine,
        "_vector_dependency_error",
        lambda: pytest.fail("small vector should not require the MVT encoder"),
    )
    engine = vector_engine.VectorEngine(
        base_url="http://127.0.0.1:1234",
        token="test-token",
        locator=lambda candidate, _target: candidate,
    )
    monkeypatch.setattr(engine, "_describe", lambda **_kwargs: None)

    assert engine.try_describe(
        {
            "target": str(source),
            "artifact_id": "vector-1",
        }
    ) is None


def test_small_local_vector_keeps_oneshot_fallback_when_ui_supplies_proxy(
    tmp_path,
):
    map_render = _load("map_render")
    source = tmp_path / "small.gpkg"
    source.write_bytes(b"small")

    assert not map_render._requires_vector_service(
        str(source),
        "http://127.0.0.1:1777/api/fs/raw?path=small.gpkg",
    )


def test_map_daemon_follower_never_steals_a_fresh_start_lock(
    monkeypatch,
    tmp_path,
):
    map_render = _load("map_render")
    start_lock = tmp_path / "daemon-start.lock"
    start_lock.write_text("owner", encoding="utf-8")
    monkeypatch.setattr(map_render, "START_LOCK", start_lock)
    monkeypatch.setattr(map_render, "_read_state", lambda: None)
    monkeypatch.setattr(map_render, "_claim_start_lock", lambda: False)
    monkeypatch.setattr(map_render, "_wait_for_service", lambda _timeout: None)

    with pytest.raises(RuntimeError, match="already in progress"):
        map_render._ensure_service()

    assert start_lock.read_text(encoding="utf-8") == "owner"
    assert (
        map_render.FOLLOWER_WAIT_TIMEOUT
        > map_render.SERVICE_START_TIMEOUT
    )
    assert (
        map_render.START_LOCK_STALE_AFTER
        > map_render.FOLLOWER_WAIT_TIMEOUT
    )


def test_remote_table_urls_are_not_converted_to_local_paths(monkeypatch):
    classify = _load("geo_classify")
    observed = []

    def capture(path, _artifact_dir, _artifact_id, _opts):
        observed.append(path)
        return {"status": "captured"}

    monkeypatch.setattr(classify, "_from_table", capture)
    monkeypatch.setattr(classify, "_from_parquet", capture)

    csv_url = "HTTPS://example.test/Data/Points.csv?version=2"
    parquet_url = "S3://Bucket/Data/Points.parquet"
    classify._from_path(csv_url, "", "csv", {})
    classify._from_path(parquet_url, "", "parquet", {})

    assert observed == [
        "https://example.test/Data/Points.csv?version=2",
        "s3://Bucket/Data/Points.parquet",
    ]


def test_remote_csv_query_string_still_selects_the_csv_reader(monkeypatch):
    classify = _load("geo_classify")
    observed = []
    fake_pandas = SimpleNamespace(
        read_csv=lambda path, **kwargs: observed.append((path, kwargs))
        or "frame",
        read_excel=lambda path: pytest.fail(
            f"Excel reader should not handle CSV URL {path}"
        ),
    )
    monkeypatch.setitem(sys.modules, "pandas", fake_pandas)
    monkeypatch.setattr(
        classify,
        "_from_dataframe",
        lambda frame, *_args, **_kwargs: {"frame": frame},
    )

    url = "https://example.test/data/points.csv?version=2"
    result = classify._from_table(url, "", "csv", {})

    assert result == {"frame": "frame"}
    assert observed == [(url, {})]


def test_map_render_retries_after_a_transient_error(tmp_path, monkeypatch):
    map_render = _load("map_render")
    target = tmp_path / "target.py"
    target.write_text("def main():\n    return None\n", encoding="utf-8")
    cache = tmp_path / "cache"
    cache.mkdir()
    attempts = []

    monkeypatch.setattr(map_render, "CACHE_DIR", cache)
    monkeypatch.setattr(map_render, "ARTIFACT_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(map_render, "_ensure_service", lambda: {})

    def describe(_state, _path, _request):
        attempts.append(True)
        if len(attempts) == 1:
            return {
                "status": "error",
                "kind": None,
                "data": {},
                "message": "temporary mount failure",
                "warnings": [],
            }
        return {
            "status": "ok",
            "kind": "vector_geojson",
            "bounds": [0, 0, 1, 1],
            "data": {},
            "warnings": [],
        }

    monkeypatch.setattr(map_render, "_post", describe)

    first = map_render.main(target=str(target))
    recovered = map_render.main(target=str(target))

    assert first["status"] == "error"
    assert recovered["status"] == "ok"
    assert len(attempts) == 2


def test_raster_service_failure_never_uses_unsafe_oneshot(
    tmp_path, monkeypatch
):
    map_render = _load("map_render")
    target = tmp_path / "managed-raster.tif"
    target.write_bytes(b"raster-placeholder")
    cache = tmp_path / "cache"

    monkeypatch.setattr(map_render, "CACHE_DIR", cache)
    monkeypatch.setattr(map_render, "ARTIFACT_DIR", cache / "artifacts")
    monkeypatch.setattr(
        map_render,
        "_ensure_service",
        lambda: (_ for _ in ()).throw(RuntimeError("daemon unavailable")),
    )
    monkeypatch.setattr(
        map_render,
        "_run_oneshot",
        lambda _request: pytest.fail("raster must not use one-shot mode"),
    )

    descriptor = map_render.main(target=str(target))

    assert descriptor["status"] == "error"
    assert descriptor["detected_type"] == "raster"
    assert "range-first raster service is unavailable" in descriptor["message"]
    assert "one-shot fallback was not used" in descriptor["message"]


def test_non_raster_service_failure_keeps_oneshot_fallback(
    tmp_path, monkeypatch
):
    map_render = _load("map_render")
    target = tmp_path / "features.geojson"
    target.write_text('{"type":"FeatureCollection","features":[]}')
    cache = tmp_path / "cache"
    fallback = {
        "id": "fallback",
        "status": "ok",
        "kind": "vector",
        "bounds": None,
        "data": {},
        "warnings": [],
        "detected_type": "vector",
        "message": "",
    }
    requests = []

    monkeypatch.setattr(map_render, "CACHE_DIR", cache)
    monkeypatch.setattr(map_render, "ARTIFACT_DIR", cache / "artifacts")
    monkeypatch.setattr(
        map_render,
        "_ensure_service",
        lambda: (_ for _ in ()).throw(RuntimeError("daemon unavailable")),
    )
    monkeypatch.setattr(
        map_render,
        "_run_oneshot",
        lambda request: requests.append(request) or fallback,
    )

    descriptor = map_render.main(target=str(target))

    assert descriptor == fallback
    assert len(requests) == 1
    assert requests[0]["target"] == str(target)


def test_large_vector_service_failure_never_loads_the_whole_file(
    tmp_path, monkeypatch
):
    map_render = _load("map_render")
    target = tmp_path / "large.gpkg"
    target.write_bytes(b"x" * 128)
    cache = tmp_path / "cache"

    monkeypatch.setattr(map_render, "CACHE_DIR", cache)
    monkeypatch.setattr(map_render, "ARTIFACT_DIR", cache / "artifacts")
    monkeypatch.setattr(map_render, "VECTOR_ONESHOT_MAX_BYTES", 64)
    monkeypatch.setattr(
        map_render,
        "_ensure_service",
        lambda: (_ for _ in ()).throw(RuntimeError("daemon unavailable")),
    )
    monkeypatch.setattr(
        map_render,
        "_run_oneshot",
        lambda _request: pytest.fail("large vector must not use one-shot mode"),
    )

    descriptor = map_render.main(target=str(target))

    assert descriptor["status"] == "error"
    assert descriptor["detected_type"] == "vector"
    assert "bounded vector-tile service is unavailable" in descriptor["message"]
    assert "whole-file one-shot fallback was not used" in descriptor["message"]


def test_managed_vector_service_failure_never_uses_the_mount_path(
    tmp_path, monkeypatch
):
    map_render = _load("map_render")
    target = tmp_path / "managed.gpkg"
    cache = tmp_path / "cache"

    monkeypatch.setattr(map_render, "CACHE_DIR", cache)
    monkeypatch.setattr(map_render, "ARTIFACT_DIR", cache / "artifacts")
    monkeypatch.setattr(
        map_render,
        "_ensure_service",
        lambda: (_ for _ in ()).throw(RuntimeError("daemon unavailable")),
    )
    monkeypatch.setattr(
        map_render,
        "_run_oneshot",
        lambda _request: pytest.fail("managed vector must not use one-shot mode"),
    )

    descriptor = map_render.main(
        target=str(target),
        source_url="http://127.0.0.1:1777/api/fs/raw?path=managed.gpkg",
    )

    assert descriptor["status"] == "error"
    assert descriptor["detected_type"] == "vector"
    assert "whole-file one-shot fallback was not used" in descriptor["message"]


def test_map_frontend_uses_native_maplibre_vector_tiles():
    template = (MAP / "template.html").read_text(encoding="utf-8")

    assert 'd.kind === "vector_tiles_mvt"' in template
    assert 'type: "vector"' in template
    assert '"source-layer": sourceLayer' in template
    assert "rebuildVectorTiles()" in template
    assert 'map.on("idle"' in template


def test_vector_engine_does_not_require_the_pure_python_mvt_stack():
    vector_engine = _load("vector_engine")

    assert "pyarrow" in vector_engine.VECTOR_RUNTIME
    assert "mapbox_vector_tile" not in vector_engine.VECTOR_RUNTIME
    assert "google.protobuf" not in vector_engine.VECTOR_RUNTIME
    assert "pyclipper" not in vector_engine.VECTOR_RUNTIME
