"""Packaging and process contracts for the built-in Map Viewer runtime."""
from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path


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


def test_map_uses_the_bundled_runtime_without_first_open_installation():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    bundled = project["project"]["optional-dependencies"]["bundled"]

    assert any(item.startswith("rio-tiler==") for item in bundled)
    assert any(item.startswith("xlrd") for item in bundled)

    launcher = (MAP / "map_render.py").read_text(encoding="utf-8")
    assert "[sys.executable," in launcher
    assert "CREATE_NO_WINDOW" in launcher
    assert "uv run" not in launcher
    assert "FUSED_RENDER_UV" not in launcher
    assert not (MAP / "pyproject.toml").exists()


def test_macos_bundle_forces_dynamic_map_runtime_packages():
    setup = (ROOT / "scripts" / "setup_py2app.py").read_text(encoding="utf-8")
    for package in (
        "rasterio",
        "rio_tiler",
        "morecantile",
        "color_operations",
        "pystac",
        "httpx2",
        "httpcore2",
        "xlrd",
    ):
        assert f'"{package}"' in setup
