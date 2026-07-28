"""Packaging and process contracts for the built-in Map Viewer runtime."""
from __future__ import annotations

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
    spec = importlib.util.spec_from_file_location(name, MAP / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
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

    for remote in (
        "https://example.test/$MAP_OBJECT/%MAP_OBJECT%/scene.tif",
        "s3://bucket/$MAP_OBJECT/%MAP_OBJECT%/scene.gpkg",
        "/vsicurl/https://example.test/$MAP_OBJECT/scene.fgb",
    ):
        assert discover.clean_path(f'"{remote}"') == remote
        assert map_render._clean_target(f'"{remote}"') == remote


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


def test_map_uses_the_bundled_runtime_without_first_open_installation():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    bundled = project["project"]["optional-dependencies"]["bundled"]

    assert any(item.startswith("rio-tiler==") for item in bundled)
    assert "mapbox-vector-tile==2.2.0" in bundled
    assert any(item.startswith("xlrd") for item in bundled)

    launcher = (MAP / "map_render.py").read_text(encoding="utf-8")
    assert "[sys.executable," in launcher
    assert "CREATE_NO_WINDOW" in launcher
    assert "uv run" not in launcher
    assert "FUSED_RENDER_UV" not in launcher
    assert not (MAP / "pyproject.toml").exists()


def test_large_vector_reports_an_old_runtime_before_registering_tiles(
    monkeypatch,
):
    vector_engine = _load("vector_engine")
    monkeypatch.setattr(
        vector_engine,
        "_encoder_dependency_error",
        lambda: "This Fused Render runtime is too old for streamed vector tiles.",
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
    assert "runtime is too old" in descriptor["message"]
    assert engine.sources == {}


def test_small_vector_can_use_geojson_fallback_on_an_old_runtime(
    monkeypatch,
    tmp_path,
):
    vector_engine = _load("vector_engine")
    source = tmp_path / "small.gpkg"
    source.write_bytes(b"small")
    monkeypatch.setattr(
        vector_engine,
        "_encoder_dependency_error",
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


def test_macos_bundle_forces_dynamic_map_runtime_packages():
    setup = (ROOT / "scripts" / "setup_py2app.py").read_text(encoding="utf-8")
    for package in (
        "rasterio",
        "rio_tiler",
        "morecantile",
        "color_operations",
        "pystac",
        "mapbox_vector_tile",
        "httpx2",
        "httpcore2",
        "xlrd",
    ):
        assert f'"{package}"' in setup


def test_remote_table_urls_are_not_converted_to_local_paths(monkeypatch):
    classify = _load("geo_classify")
    observed = []

    def capture(path, _artifact_dir, _artifact_id, _opts):
        observed.append(path)
        return {"status": "captured"}

    monkeypatch.setattr(classify, "_from_table", capture)
    monkeypatch.setattr(classify, "_from_parquet", capture)

    csv_url = "https://example.test/data/points.csv?version=2"
    parquet_url = "s3://bucket/data/points.parquet"
    classify._from_path(csv_url, "", "csv", {})
    classify._from_path(parquet_url, "", "parquet", {})

    assert observed == [csv_url, parquet_url]


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
