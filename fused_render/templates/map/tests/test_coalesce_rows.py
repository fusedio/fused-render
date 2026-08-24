"""Sub-pixel feature coalescing in dense detail tiles (vector_engine.py).

_coalesce_rows is only reached from _detail_tile with >256 features of which
>256 are smaller than a coalesce cell, so the ordinary vector-tile tests (which
draw the overview, or stay under the 256 guard) never exercise its cell packing,
largest-per-cell selection, or row-index handling. These drive it directly.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_coalesce_rows.py -o addopts=""
"""
import importlib.util
import os
import sys

import numpy as np
import pytest

_MAP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHARED = os.path.join(os.path.dirname(_MAP), "shared")


def _load(name, filename):
    for path in (_SHARED, _MAP):
        if path not in sys.path:
            sys.path.insert(0, path)
    spec = importlib.util.spec_from_file_location(name, os.path.join(_MAP, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ve():
    return _load("map_vector_engine", "vector_engine.py")


@pytest.fixture
def eng(ve):
    return ve.VectorEngine(
        base_url="http://x", token="t", locator=lambda source, target: source
    )


def _boxes(ve, n_cells):
    """Two boxes per cell (a small and a larger one, both under the cell in both
    dimensions) laid out one cell apart, plus a handful of features larger than a
    cell. Returns the geometry array and the row indices we expect kept."""
    import shapely

    cell = ve.MVT_EXTENT / ve.TILE_PIXELS * ve.COALESCE_PIXELS
    geoms, expected = [], set()
    for k in range(n_cells):
        center = k * cell + cell / 2.0
        small = 0.15 * cell
        large = 0.30 * cell
        geoms.append(shapely.box(center - small, cell / 2 - small,
                                 center + small, cell / 2 + small))
        big_row = len(geoms)
        geoms.append(shapely.box(center - large, cell / 2 - large,
                                 center + large, cell / 2 + large))
        expected.add(big_row)  # the larger box in each cell must win
    # extended features (wider than a cell): never candidates, always kept.
    for _ in range(3):
        expected.add(len(geoms))
        geoms.append(shapely.box(0.0, 200.0, 3.0 * cell, 260.0))
    return np.array(geoms, dtype=object), expected


def test_keeps_largest_per_cell_and_all_large_features(ve, eng):
    geometries, expected = _boxes(ve, n_cells=300)  # 600 small + 3 large
    drawable = np.ones(len(geometries), dtype=bool)

    rows = eng._coalesce_rows(geometries, drawable)

    assert set(int(r) for r in rows) == expected
    assert list(rows) == sorted(rows)  # paint order preserved (ascending)
    assert len(rows) == 303


def test_distinct_cells_do_not_collide(ve, eng):
    # One box per cell across many cells: every feature occupies its own cell, so
    # nothing may be coalesced away (guards against a packing collision).
    import shapely

    cell = ve.MVT_EXTENT / ve.TILE_PIXELS * ve.COALESCE_PIXELS
    geoms = [
        shapely.box(k * cell + 1, 1.0, k * cell + 3, 3.0) for k in range(400)
    ]
    geometries = np.array(geoms, dtype=object)
    drawable = np.ones(len(geometries), dtype=bool)

    rows = eng._coalesce_rows(geometries, drawable)

    assert len(rows) == 400


def test_below_guard_returns_all_drawable(ve, eng):
    import shapely

    geoms = [shapely.box(i, 0.0, i + 1, 1.0) for i in range(100)]
    geometries = np.array(geoms, dtype=object)
    drawable = np.ones(len(geometries), dtype=bool)
    drawable[10] = False  # a missing/empty geometry is never drawn

    rows = eng._coalesce_rows(geometries, drawable)

    assert list(rows) == [i for i in range(100) if i != 10]
