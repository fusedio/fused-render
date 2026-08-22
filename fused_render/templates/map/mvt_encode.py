"""Minimal Mapbox Vector Tile writer.

Tiles used to go through GDAL's MVT driver, which builds a whole tileset
(scratch dataset, per-call temporary directory, sqlite spill) for every single
tile — measured at ~0.9s of fixed overhead per tile on a large GeoPackage. A
tile here is one layer of at most a few thousand small features, so the
protobuf (vector_tile.proto, spec 2.1) is written directly.
"""
from __future__ import annotations

import struct
from typing import Any, Iterable

POINT = 1
LINESTRING = 2
POLYGON = 3

MOVE_TO = 1
LINE_TO = 2
CLOSE_PATH = 7


def _varint(out: bytearray, value: int) -> None:
    while True:
        low = value & 0x7F
        value >>= 7
        if value:
            out.append(low | 0x80)
        else:
            out.append(low)
            return


def zigzag(value: int) -> int:
    return (value << 1) ^ (value >> 63)


def _key(out: bytearray, number: int, wire: int) -> None:
    _varint(out, (number << 3) | wire)


def _blob(out: bytearray, number: int, payload: bytes) -> None:
    _key(out, number, 2)
    _varint(out, len(payload))
    out.extend(payload)


def _value(value: Any) -> bytes:
    out = bytearray()
    if isinstance(value, bool):
        _key(out, 7, 0)
        _varint(out, int(value))
    elif isinstance(value, int):
        _key(out, 6, 0)
        _varint(out, zigzag(value))
    elif isinstance(value, float):
        _key(out, 3, 1)
        out.extend(struct.pack("<d", value))
    else:
        _blob(out, 1, str(value).encode("utf-8"))
    return bytes(out)


def path_commands(
    geometry: list[int],
    points: Iterable[tuple[int, int]],
    cursor: list[int],
    close: bool,
) -> None:
    """One MoveTo/LineTo[/ClosePath] path. `points` are absolute integer tile
    coordinates with consecutive duplicates already removed; the cursor carries
    delta state across every path of a feature."""
    points = list(points)
    x, y = points[0]
    geometry.append((1 << 3) | MOVE_TO)
    geometry.append(zigzag(x - cursor[0]))
    geometry.append(zigzag(y - cursor[1]))
    cursor[0], cursor[1] = x, y
    if len(points) > 1:
        geometry.append(((len(points) - 1) << 3) | LINE_TO)
        for x, y in points[1:]:
            geometry.append(zigzag(x - cursor[0]))
            geometry.append(zigzag(y - cursor[1]))
            cursor[0], cursor[1] = x, y
    if close:
        geometry.append((1 << 3) | CLOSE_PATH)


def point_commands(
    geometry: list[int],
    points: Iterable[tuple[int, int]],
    cursor: list[int],
) -> None:
    points = list(points)
    geometry.append((len(points) << 3) | MOVE_TO)
    for x, y in points:
        geometry.append(zigzag(x - cursor[0]))
        geometry.append(zigzag(y - cursor[1]))
        cursor[0], cursor[1] = x, y


class LayerWriter:
    def __init__(self, name: str, extent: int):
        self.name = name
        self.extent = extent
        self.features: list[bytes] = []
        self.keys: dict[str, int] = {}
        self.values: dict[tuple[str, Any], int] = {}
        self.value_blobs: list[bytes] = []

    def _tags(self, properties: dict[str, Any]) -> list[int]:
        tags: list[int] = []
        for key, value in properties.items():
            if value is None:
                continue
            if not isinstance(value, (bool, int, float, str)):
                value = str(value)
            key_index = self.keys.setdefault(key, len(self.keys))
            value_key = (type(value).__name__, value)
            value_index = self.values.get(value_key)
            if value_index is None:
                value_index = self.values[value_key] = len(self.value_blobs)
                self.value_blobs.append(_value(value))
            tags.append(key_index)
            tags.append(value_index)
        return tags

    def feature(
        self,
        geometry_type: int,
        geometry: list[int],
        properties: dict[str, Any],
    ) -> None:
        body = bytearray()
        tags = self._tags(properties)
        if tags:
            packed = bytearray()
            for tag in tags:
                _varint(packed, tag)
            _blob(body, 2, bytes(packed))
        _key(body, 3, 0)
        _varint(body, geometry_type)
        packed = bytearray()
        for value in geometry:
            _varint(packed, value)
        _blob(body, 4, bytes(packed))
        self.features.append(bytes(body))

    def tile(self) -> bytes:
        if not self.features:
            return b""
        layer = bytearray()
        _key(layer, 15, 0)
        _varint(layer, 2)
        _blob(layer, 1, self.name.encode("utf-8"))
        for feature in self.features:
            _blob(layer, 2, feature)
        for key in self.keys:
            _blob(layer, 3, key.encode("utf-8"))
        for blob in self.value_blobs:
            _blob(layer, 4, blob)
        _key(layer, 5, 0)
        _varint(layer, self.extent)
        tile = bytearray()
        _blob(tile, 3, bytes(layer))
        return bytes(tile)
