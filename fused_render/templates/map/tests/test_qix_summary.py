"""The .qix quadtree overview path (vector_engine.py).

A large shapefile used to stall on first open because the low-zoom overview was
built by reading every geometry (a ~25s full-file read on a 394MB .shp, then
disk-cached — so only the first open hung). When the shapefile ships a .qix
spatial index, the overview is now read from that few-MB index instead, the
same way GeoPackage sources walk their RTree nodes.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_qix_summary.py -o addopts=""
"""
import importlib.util
import os
import struct
import sys
import types

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


def _node(bounds, ids, subnodes):
    return (
        struct.pack("<i", 0)
        + struct.pack("<4d", *bounds)
        + struct.pack("<i", len(ids))
        + b"".join(struct.pack("<i", i) for i in ids)
        + struct.pack("<i", subnodes)
    )


def _write_qix(path, nshapes):
    # root (holds shape 0) with two leaf children.
    body = (
        _node((0, 0, 10, 10), [0], 2)
        + _node((0, 0, 5, 5), [1, 2], 0)
        + _node((5, 5, 10, 10), [3], 0)
    )
    header = b"SQT" + bytes([1, 1, 0, 0, 0]) + struct.pack("<ii", nshapes, 2)
    with open(path, "wb") as handle:
        handle.write(header + body)


def test_parse_qix_reads_shape_bearing_node_boxes(eng, tmp_path):
    qix = tmp_path / "farms.qix"
    _write_qix(qix, nshapes=4)

    minx, maxx, miny, maxy, weights = eng._parse_qix(str(qix), 4)

    # All three nodes carry shapes, in file order: root, then the two leaves.
    assert list(minx) == [0, 0, 5]
    assert list(maxx) == [10, 5, 10]
    assert list(miny) == [0, 0, 5]
    assert list(maxy) == [10, 5, 10]
    # Each node keeps its own shape count: root holds 1, the leaves 2 and 1.
    assert list(weights) == [1, 2, 1]


def test_parse_qix_rejects_a_non_index_file(eng, tmp_path):
    bad = tmp_path / "bad.qix"
    bad.write_bytes(b"NOTAQIX!" + b"\x00" * 16)
    with pytest.raises(ValueError):
        eng._parse_qix(str(bad), 4)


def test_parse_qix_rejects_a_node_count_inconsistent_with_the_layer(eng, tmp_path):
    # 3 shape-bearing nodes but a claimed 100000 features: 100000/500=200 > 3.
    qix = tmp_path / "farms.qix"
    _write_qix(qix, nshapes=100000)
    with pytest.raises(ValueError):
        eng._parse_qix(str(qix), 100000)


def test_qix_summary_used_when_the_index_exists(eng, tmp_path):
    shp = tmp_path / "farms.shp"
    shp.write_bytes(b"")  # only its presence is checked; the .qix is read
    _write_qix(tmp_path / "farms.qix", nshapes=4)
    source = types.SimpleNamespace(locator=str(shp), feature_count=4)

    summary = eng._qix_summary(source)

    assert summary is not None
    assert len(summary[0]) == 3


def test_qix_summary_finds_uppercase_index(eng, tmp_path):
    # ESRI tools name the sidecar to match the shapefile case (.SHP -> .QIX);
    # on a case-sensitive filesystem a lowercase-only lookup would miss it.
    shp = tmp_path / "farms.shp"
    shp.write_bytes(b"")
    _write_qix(tmp_path / "farms.QIX", nshapes=4)
    source = types.SimpleNamespace(locator=str(shp), feature_count=4)

    summary = eng._qix_summary(source)

    assert summary is not None
    assert len(summary[0]) == 3


def test_qix_summary_absent_index_falls_back(eng, tmp_path):
    shp = tmp_path / "farms.shp"
    shp.write_bytes(b"")  # no sibling .qix
    source = types.SimpleNamespace(locator=str(shp), feature_count=4)

    assert eng._qix_summary(source) is None
