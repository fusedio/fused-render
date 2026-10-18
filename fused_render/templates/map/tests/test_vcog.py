from __future__ import annotations

import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
rasterio = pytest.importorskip("rasterio")
from rasterio.io import MemoryFile  # noqa: E402
from rasterio.transform import from_bounds  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vcog import VirtualCOG  # noqa: E402


TILED = {"tiled": True, "blockxsize": 16, "blockysize": 16}


@pytest.mark.parametrize("dtype, options", [
    ("uint8", {"compress": "lzw", "nbits": 4, **TILED}),
    ("int16", {"compress": "deflate", "predictor": 2, **TILED}),
    ("float32", TILED),
    ("float32", {"tiled": False}),
])
def test_a_virtual_cog_reads_back_like_its_source(tmp_path, dtype, options):
    height, width = 300, 2100
    data = (np.arange(height * width).reshape(height, width) // 37 % 13).astype(dtype)
    source = tmp_path / "source.tif"
    with rasterio.open(source, "w", driver="GTiff", width=width, height=height, count=1, dtype=dtype, nodata=15,
                       crs="EPSG:32633", transform=from_bounds(500000, 5000000, 521000, 5003000, width, height),
                       **options) as out:
        out.write(data, 1)
    virtual = VirtualCOG(str(source))
    with MemoryFile(virtual.read(0, virtual.size)) as memory, memory.open() as cog, rasterio.open(source) as original:
        assert (cog.crs, cog.transform, cog.nodata) == (original.crs, original.transform, original.nodata)
        assert np.array_equal(cog.read(1), data) and cog.overviews(1) == [2, 4, 8]
        coarse = cog.read(1, out_shape=(height // 8 + 1, width // 8 + 1))
    rows, cols = (np.arange(coarse.shape[0]) * 8 // 16) * 16, (np.arange(coarse.shape[1]) * 8 // 16) * 16
    assert np.array_equal(coarse, data[rows][:, cols]) if virtual.sample else set(np.unique(coarse)) == set(np.unique(data))


def test_a_source_without_an_epsg_crs_is_served_in_wgs84(tmp_path):
    source = tmp_path / "mollweide.tif"
    with rasterio.open(source, "w", driver="GTiff", width=256, height=256, count=1, dtype="float32",
                       crs="ESRI:54009", transform=from_bounds(3.2e6, 2.0e6, 5.3e6, 3.9e6, 256, 256), nodata=-1) as out:
        out.write(np.ones((1, 256, 256), "float32"))
    virtual = VirtualCOG(str(source))
    with MemoryFile(virtual.read(0, virtual.size)) as memory, memory.open() as cog:
        assert cog.crs.to_epsg() == 4326 and 30 < cog.bounds.left < cog.bounds.right < 60


def test_coarse_levels_of_a_class_raster_keep_the_majority_class(tmp_path):
    data = np.full((1100, 1100), 11, "uint8")
    data[::4, ::4], data[:, :300] = 13, 0
    source = tmp_path / "classes.tif"
    with rasterio.open(source, "w", driver="GTiff", width=1100, height=1100, count=1, dtype="uint8", nodata=0,
                       crs="EPSG:4326", transform=from_bounds(50, 20, 51, 21, 1100, 1100), compress="zstd") as out:
        out.write(data, 1)
    virtual = VirtualCOG(str(source))
    with MemoryFile(virtual.read(0, virtual.size)) as memory, memory.open() as cog:
        coarse = cog.read(1, out_shape=(275, 275))
    assert set(np.unique(coarse[:, 80:])) == {11} and set(np.unique(coarse[:, :70])) == {0}
