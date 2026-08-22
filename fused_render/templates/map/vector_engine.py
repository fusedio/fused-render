"""Native, bounded vector tiles for Map Viewer.

Large vectors are never serialized wholesale for the browser. GeoPackage
sources use their RTree for bounded reads. Detailed tiles are encoded by
mvt_encode's direct protobuf writer, while tiles that contain more geometry
than a screen can distinguish become occupancy overviews instead of arbitrary
feature samples. Generated tiles are cached in memory and on disk.
"""
from __future__ import annotations

import contextlib
import hashlib
import math
import os
import sqlite3
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

from geo_paths import (
    is_http_url,
    is_remote_path,
    normalize_remote_path,
    resolve_source,
)
from mvt_encode import LINESTRING, POINT, POLYGON, LayerWriter, path_commands, point_commands
from optional_runtime import require
from raster_engine import error_descriptor


VECTOR_SUFFIXES = {
    ".geojson",
    ".json",
    ".shp",
    ".gpkg",
    ".fgb",
    ".kml",
    ".gml",
}
VECTOR_TILE_MIN_BYTES = int(
    os.environ.get("MAP_VIEWER_VECTOR_TILE_MIN_BYTES", str(32 << 20))
)
VECTOR_TILE_MIN_FEATURES = int(
    os.environ.get("MAP_VIEWER_VECTOR_TILE_MIN_FEATURES", "50000")
)
MAX_TILE_FEATURES = int(
    os.environ.get("MAP_VIEWER_VECTOR_TILE_FEATURES", "5000")
)
OVERVIEW_GRID_SIZE = int(
    os.environ.get("MAP_VIEWER_VECTOR_OVERVIEW_GRID", "64")
)
# Above this many features in a tile's bbox the exact per-cell SQL count is
# retired for the rtree node summary (~1.3µs/feature measured: 400k ≈ 0.5s).
OVERVIEW_EXACT_MAX = int(
    os.environ.get("MAP_VIEWER_VECTOR_OVERVIEW_EXACT", "400000")
)
MAX_TILE_CACHE = int(
    os.environ.get("MAP_VIEWER_VECTOR_TILE_CACHE_SIZE", "512")
)
MAX_ATTRIBUTES = int(
    os.environ.get("MAP_VIEWER_VECTOR_TILE_ATTRIBUTES", "8")
)
MVT_EXTENT = 4096
MVT_BUFFER = 64
SIMPLIFY_TOLERANCE = 2.0
WEB_MERCATOR_LIMIT = math.pi * 6378137.0
MAX_LATITUDE = 85.0511287798066
ENGINE_VERSION = "native-mvt-v2"
# The MVT tile pyramid's {z}/{x}/{y} math is fixed to EPSG:4326 — this is this
# pipeline's "canvas CRS" (the same role QGIS's project CRS plays: every layer,
# regardless of its own native CRS, is reprojected on-the-fly for that one
# render/tile). The source file's CRS is never touched.
TILE_CRS = "EPSG:4326"


VECTOR_RUNTIME = {
    "geopandas": "geopandas",
    "pyarrow": "pyarrow",
    "pyogrio": "pyogrio",
    "pyproj": "pyproj",
    "rasterio": "rasterio",
    "shapely": "shapely",
}


class TileCancelled(Exception):
    """The tile's client went away mid-render; the work was abandoned."""


def _vector_dependency_error() -> str | None:
    return require("Streamed vector layers", VECTOR_RUNTIME)


def _suffix(value: str) -> str:
    path = urlsplit(value).path if is_http_url(value) else value
    return Path(path.replace("\\", "/")).suffix.lower()


def _source_size(source: str) -> int | None:
    if is_remote_path(source):
        return None
    try:
        return os.path.getsize(source)
    except OSError:
        return None


def _source_fingerprint(locator: str) -> str:
    if not os.path.isfile(locator):
        return locator
    try:
        stat = os.stat(locator)
        return f"{os.path.abspath(locator)}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        return os.path.abspath(locator)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _finite_bounds(value: Any) -> list[float] | None:
    try:
        bounds = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if len(bounds) != 4 or not all(math.isfinite(item) for item in bounds):
        return None
    return bounds


def _geometry_family(value: str) -> str:
    lowered = value.lower()
    if "point" in lowered:
        return "point"
    if "line" in lowered or "curve" in lowered:
        return "line"
    if "polygon" in lowered or "surface" in lowered:
        return "polygon"
    return "mixed"


def _default_style(family: str) -> dict[str, Any]:
    style = {
        "opacity": 1.0,
        "fill_color": [56, 135, 255, 90],
        "line_color": [90, 200, 255, 220],
        "line_width": 1.5,
        "point_radius": 5,
        "color_by": None,
        "colormap": "viridis",
    }
    if family == "point":
        style["fill_color"] = [0, 200, 255, 220]
    return style


def _tile_bounds_4326(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    scale = 1 << z
    west = x / scale * 360.0 - 180.0
    east = (x + 1) / scale * 360.0 - 180.0
    north = math.degrees(
        math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / scale)))
    )
    south = math.degrees(
        math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + 1) / scale)))
    )
    return west, south, east, north


def _intersects(
    left: list[float] | tuple[float, float, float, float],
    right: list[float] | tuple[float, float, float, float],
) -> bool:
    return not (
        left[2] < right[0]
        or left[0] > right[2]
        or left[3] < right[1]
        or left[1] > right[3]
    )


def _buffered_bounds(
    bounds: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    longitude = (bounds[2] - bounds[0]) * MVT_BUFFER / MVT_EXTENT
    latitude = (bounds[3] - bounds[1]) * MVT_BUFFER / MVT_EXTENT
    return (
        max(-180.0, bounds[0] - longitude),
        max(-MAX_LATITUDE, bounds[1] - latitude),
        min(180.0, bounds[2] + longitude),
        min(MAX_LATITUDE, bounds[3] + latitude),
    )


def _tile_units(lon, lat, z: int, x: int, y: int):
    """EPSG:4326 coordinates to this tile's integer-quantizable MVT units.
    Mercator y is linear in tile y, so the tile fraction is computed directly."""
    import numpy as np

    scale = float(1 << z)
    tx = ((np.asarray(lon) + 180.0) / 360.0 * scale - x) * MVT_EXTENT
    lat_r = np.radians(np.clip(np.asarray(lat), -MAX_LATITUDE, MAX_LATITUDE))
    ty = ((1.0 - np.arcsinh(np.tan(lat_r)) / math.pi) / 2.0 * scale - y) * MVT_EXTENT
    return tx, ty


def _int_path(coords, ring: bool):
    """Quantize one path to integer tile units, dropping the closing point of a
    ring and consecutive duplicates. None when the path degenerates."""
    import numpy as np

    quantized = np.rint(coords).astype(np.int64)
    if ring and len(quantized) > 1 and (quantized[0] == quantized[-1]).all():
        quantized = quantized[:-1]
    if len(quantized) > 1:
        keep = np.empty(len(quantized), dtype=bool)
        keep[0] = True
        keep[1:] = (quantized[1:] != quantized[:-1]).any(axis=1)
        quantized = quantized[keep]
    if ring and len(quantized) > 1 and (quantized[0] == quantized[-1]).all():
        quantized = quantized[:-1]
    if len(quantized) < (3 if ring else 2):
        return None
    return quantized


def _shoelace2(points) -> int:
    import numpy as np

    x, y = points[:, 0], points[:, 1]
    return int(x @ np.roll(y, -1) - np.roll(x, -1) @ y)


def _polygon_rings(polygon):
    """Quantized rings of one polygon, wound for MVT (in y-down tile units the
    surveyor's formula gives an exterior a positive area, interiors negative).
    A degenerate exterior voids the polygon."""
    rings = []
    import shapely

    for index, ring in enumerate([polygon.exterior, *polygon.interiors]):
        points = _int_path(shapely.get_coordinates(ring), ring=True)
        area = 0 if points is None else _shoelace2(points)
        if area == 0:
            if index == 0:
                return []
            continue
        if (area > 0) != (index == 0):
            points = points[::-1]
        rings.append(points)
    return rings


def _mvt_features(geometry):
    """(geometry_type, command_integers) features for one shapely geometry."""
    import numpy as np
    import shapely

    type_id = shapely.get_type_id(geometry)
    if type_id in (0, 4):
        points = np.rint(shapely.get_coordinates(geometry)).astype(np.int64)
        if len(points):
            commands: list[int] = []
            point_commands(commands, points.tolist(), [0, 0])
            yield POINT, commands
    elif type_id in (1, 2, 5):
        commands = []
        cursor = [0, 0]
        parts = shapely.get_parts(geometry) if type_id == 5 else [geometry]
        for part in parts:
            points = _int_path(shapely.get_coordinates(part), ring=False)
            if points is not None:
                path_commands(commands, points.tolist(), cursor, close=False)
        if commands:
            yield LINESTRING, commands
    elif type_id in (3, 6):
        commands = []
        cursor = [0, 0]
        parts = shapely.get_parts(geometry) if type_id == 6 else [geometry]
        for part in parts:
            for points in _polygon_rings(part):
                path_commands(commands, points.tolist(), cursor, close=True)
        if commands:
            yield POLYGON, commands
    elif type_id == 7:
        for part in shapely.get_parts(geometry):
            yield from _mvt_features(part)


def _coverage_grid(minx, maxx, miny, maxy, weight: float, bbox, size: int):
    """Mark every grid cell each node bbox overlaps (difference array + 2D
    prefix sum), so summary coverage has no holes between node centres."""
    import numpy as np

    span_x = bbox[2] - bbox[0]
    span_y = bbox[3] - bbox[1]
    ix0 = np.clip(((minx - bbox[0]) / span_x * size).astype(np.int64), 0, size - 1)
    ix1 = np.clip(((maxx - bbox[0]) / span_x * size).astype(np.int64), 0, size - 1)
    iy0 = np.clip(((miny - bbox[1]) / span_y * size).astype(np.int64), 0, size - 1)
    iy1 = np.clip(((maxy - bbox[1]) / span_y * size).astype(np.int64), 0, size - 1)
    diff = np.zeros((size + 1, size + 1))
    np.add.at(diff, (ix0, iy0), weight)
    np.add.at(diff, (ix1 + 1, iy0), -weight)
    np.add.at(diff, (ix0, iy1 + 1), -weight)
    np.add.at(diff, (ix1 + 1, iy1 + 1), weight)
    return diff.cumsum(axis=0).cumsum(axis=1)[:size, :size]


@contextlib.contextmanager
def _gdal_env():
    import pyogrio
    import rasterio

    # A .shp dragged in without its .shx sidecar (browsers only upload the
    # file the user dropped) is recoverable: the .shx is a redundant index
    # GDAL can rebuild. Binary wheels give pyogrio a GDAL copy of its own
    # that rasterio.Env can't reach, so the option is set on both.
    pyogrio.set_gdal_config_options({"SHAPE_RESTORE_SHX": True})
    with rasterio.Env(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_USE_HEAD="YES",
        GDAL_HTTP_MULTIRANGE="YES",
        GDAL_HTTP_MAX_RETRY="2",
        GDAL_HTTP_RETRY_DELAY="0.2",
        CPL_VSIL_CURL_CHUNK_SIZE=str(64 << 10),
        CPL_VSIL_CURL_CACHE_SIZE=str(16 << 20),
        SHAPE_RESTORE_SHX="YES",
    ):
        yield


@dataclass
class VectorSource:
    source_id: str
    target: str
    locator: str
    layer: str
    geometry_column: str
    geometry_type: str
    family: str
    crs: str
    bounds: list[float]
    feature_count: int
    attributes: list[str]
    columns: dict[str, str]
    rtree_table: str | None = None
    sqlite_uri: str | None = None


class VectorEngine:
    def __init__(
        self,
        base_url: str,
        token: str,
        locator: Callable[[str, str], str],
        cache_dir: str | os.PathLike[str] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.locator = locator
        self.sources: dict[str, VectorSource] = {}
        self.lock = threading.RLock()
        self.tile_cache: OrderedDict[tuple[str, int, int, int], bytes] = (
            OrderedDict()
        )
        self.inflight: dict[tuple[str, int, int, int], threading.Event] = {}
        self._sqlite: dict[str, sqlite3.Connection] = {}
        self._summaries: dict[str, tuple | None] = {}
        self._transformers: dict[str, Any] = {}
        self.cache_dir = (
            Path(cache_dir) / "vector-tiles" / ENGINE_VERSION
            if cache_dir is not None
            else None
        )
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def try_describe(self, request: dict[str, Any], obj: Any | None = None):
        target = obj if isinstance(obj, (str, os.PathLike)) else request.get("target")
        if not isinstance(target, (str, os.PathLike)):
            return None
        target = str(target).strip()
        if is_remote_path(target):
            target = normalize_remote_path(target)
        if not target or _suffix(target) not in VECTOR_SUFFIXES:
            return None

        source = resolve_source(request, target)
        source_size = _source_size(source)
        artifact_id = str(request.get("artifact_id") or "")
        if source_size is None or source_size >= VECTOR_TILE_MIN_BYTES:
            dependency_error = _vector_dependency_error()
            if dependency_error:
                return error_descriptor(artifact_id, dependency_error, detected_type="vector")
        try:
            locator = self.locator(source, target)
            return self._describe(
                target=target,
                locator=locator,
                artifact_id=artifact_id,
                source_size=source_size,
            )
        except Exception as error:
            if source_size is not None and source_size < VECTOR_TILE_MIN_BYTES:
                return None
            return {
                "id": artifact_id,
                "status": "error",
                "kind": None,
                "bounds": None,
                "data": {},
                "stats": {},
                "style": {},
                "warnings": [],
                "detected_type": "vector",
                "message": (
                    "Large vector metadata could not be read without loading "
                    f"the whole source: {type(error).__name__}: {error}"
                ),
            }

    def _describe(
        self,
        target: str,
        locator: str,
        artifact_id: str,
        source_size: int | None,
    ) -> dict[str, Any] | None:
        import pyogrio
        from pyproj import CRS, Transformer

        with _gdal_env():
            layers = pyogrio.list_layers(locator)
            if layers is None or not len(layers):
                raise ValueError("the dataset has no vector layers")
            candidates = []
            for row in layers:
                layer = str(row[0])
                info = pyogrio.read_info(
                    locator,
                    layer=layer,
                    force_feature_count=False,
                    force_total_bounds=False,
                )
                candidates.append((int(info.get("features") or 0), layer, info))
        feature_count, layer, info = max(candidates, key=lambda item: item[0])
        if (
            source_size is not None
            and source_size < VECTOR_TILE_MIN_BYTES
            and feature_count < VECTOR_TILE_MIN_FEATURES
        ):
            return None

        dependency_error = _vector_dependency_error()
        if dependency_error:
            return error_descriptor(artifact_id, dependency_error, detected_type="vector")

        source_crs = info.get("crs")
        if not source_crs:
            raise ValueError(f"layer {layer!r} has no CRS")
        source_crs = CRS.from_user_input(source_crs)
        source_bounds = _finite_bounds(info.get("total_bounds"))
        if source_bounds is None:
            raise ValueError(f"layer {layer!r} has no finite extent metadata")
        if source_crs.to_epsg() == 4326:
            bounds = source_bounds
        else:
            bounds = list(
                Transformer.from_crs(
                    source_crs, "EPSG:4326", always_xy=True
                ).transform_bounds(*source_bounds, densify_pts=21)
            )
        bounds[1] = max(-MAX_LATITUDE, bounds[1])
        bounds[3] = min(MAX_LATITUDE, bounds[3])

        geometry_type = str(info.get("geometry_type") or "Unknown")
        family = _geometry_family(geometry_type)
        fields = [
            str(value)
            for value in ([] if info.get("fields") is None else info["fields"])
        ]
        dtypes = [
            str(value)
            for value in ([] if info.get("dtypes") is None else info["dtypes"])
        ]
        columns = dict(zip(fields, dtypes))
        attributes = fields[:MAX_ATTRIBUTES]
        geometry_column = str(info.get("geometry_name") or "geometry")
        source_id = hashlib.sha256(
            (
                f"{ENGINE_VERSION}\0{artifact_id}\0"
                f"{_source_fingerprint(locator)}\0{layer}"
            ).encode("utf-8")
        ).hexdigest()[:24]
        source = VectorSource(
            source_id=source_id,
            target=target,
            locator=locator,
            layer=layer,
            geometry_column=geometry_column,
            geometry_type=geometry_type,
            family=family,
            crs=source_crs.to_string(),
            bounds=bounds,
            feature_count=feature_count,
            attributes=attributes,
            columns=columns,
        )
        self._attach_geopackage_index(source)
        with self.lock:
            source = self.sources.setdefault(source_id, source)

        warnings = [
            (
                f"{feature_count:,} features use native, cached vector tiles. "
                "Dense views use a complete occupancy overview and switch to "
                "individual geometries as the map zooms in."
            )
        ]
        if source.rtree_table:
            warnings.append(
                "GeoPackage RTree detected; every tile uses indexed spatial reads."
            )
        else:
            warnings.append(
                "No directly queryable GeoPackage RTree was detected. The "
                "source driver's spatial filter remains bounded, but dense "
                "views may take longer."
            )
        return {
            "id": artifact_id,
            "status": "ok",
            "kind": "vector_tiles_mvt",
            "bounds": bounds,
            "minzoom": 0,
            "maxzoom": 18,
            "data": {
                "source_id": source_id,
                "source_layer": "layer",
                "tile_url": (
                    f"{self.base_url}/vtiles/{source_id}"
                    f"/{{z}}/{{x}}/{{y}}.pbf?t={quote(self.token, safe='')}"
                ),
            },
            "stats": {
                "feature_count": feature_count,
                "geometry_types": [geometry_type],
                "columns": columns,
                "numeric_columns": [
                    name
                    for name, dtype in columns.items()
                    if dtype.startswith(("int", "uint", "float"))
                ],
                "source_layer": layer,
                "indexed": bool(source.rtree_table),
                "tile_feature_limit": MAX_TILE_FEATURES,
                "native_mvt": True,
                "dense_overviews": True,
            },
            "style": _default_style(family),
            "crs_original": source.crs,
            "warnings": warnings,
            "detected_type": "large vector file",
            "message": "",
        }

    def _attach_geopackage_index(self, source: VectorSource) -> None:
        if _suffix(source.locator) != ".gpkg" or not os.path.isfile(source.locator):
            return
        try:
            uri = f"file:{Path(source.locator).resolve().as_posix()}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=30) as connection:
                row = connection.execute(
                    "SELECT column_name FROM gpkg_geometry_columns "
                    "WHERE table_name = ?",
                    (source.layer,),
                ).fetchone()
                if not row:
                    return
                rtree = f"rtree_{source.layer}_{row[0]}"
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = ?",
                    (rtree,),
                ).fetchone()
                if not exists:
                    return
            source.rtree_table = rtree
            source.sqlite_uri = uri
        except (OSError, sqlite3.Error):
            return

    def _connection(self, source: VectorSource) -> sqlite3.Connection:
        with self.lock:
            connection = self._sqlite.get(source.source_id)
            if connection is None:
                # One reader per source, reused across tiles. VTILE_POOL
                # serialises all tile work onto one persistent thread, but the
                # connection is born wherever the first query runs.
                connection = sqlite3.connect(
                    source.sqlite_uri, uri=True, timeout=30,
                    check_same_thread=False,
                )
                self._sqlite[source.source_id] = connection
        return connection

    def _query(
        self,
        source: VectorSource,
        cancel: threading.Event | None,
        sql: str,
        parameters: tuple,
    ) -> list:
        connection = self._connection(source)
        if cancel is not None:
            connection.set_progress_handler(cancel.is_set, 100_000)
        try:
            return connection.execute(sql, parameters).fetchall()
        finally:
            if cancel is not None:
                connection.set_progress_handler(None, 0)

    def _transformer(self, source: VectorSource):
        transformer = self._transformers.get(source.source_id)
        if transformer is None:
            from pyproj import Transformer

            transformer = Transformer.from_crs(
                source.crs, TILE_CRS, always_xy=True
            )
            self._transformers[source.source_id] = transformer
        return transformer

    def _source_bbox(
        self,
        source: VectorSource,
        bounds_4326: tuple[float, float, float, float],
    ) -> tuple[float, float, float, float]:
        if source.crs.upper() in {"EPSG:4326", "OGC:CRS84"}:
            return bounds_4326
        from pyproj import Transformer

        return Transformer.from_crs(
            "EPSG:4326", source.crs, always_xy=True
        ).transform_bounds(*bounds_4326, densify_pts=21)

    def _indexed_feature_ids(
        self,
        source: VectorSource,
        bbox: tuple[float, float, float, float],
        cancel: threading.Event | None = None,
    ) -> tuple[bool, list[int] | None]:
        if not source.rtree_table or not source.sqlite_uri:
            return False, None
        table = _quote_identifier(source.rtree_table)
        rows = self._query(
            source,
            cancel,
            f"SELECT id FROM {table} "
            "WHERE maxx >= ? AND minx <= ? AND maxy >= ? AND miny <= ? "
            "LIMIT ?",
            (bbox[0], bbox[2], bbox[1], bbox[3], MAX_TILE_FEATURES + 1),
        )
        dense = len(rows) > MAX_TILE_FEATURES
        return dense, None if dense else [int(row[0]) for row in rows]

    def _node_summary(self, source: VectorSource) -> tuple | None:
        """Leaf-node bboxes of the GeoPackage's RTree, read once per source.

        sqlite stores the rtree in an ordinary shadow ``<rtree>_node`` table
        (blob format: 2-byte depth on the root, 2-byte cell count, then
        {8-byte big-endian id, 4 big-endian f32 coords} cells). Walking the
        internal levels takes ~0.4s on 13.8M features where the equivalent
        virtual-table scan takes 16s. Any surprise in the format drops the
        summary and tiles fall back to the exact per-cell SQL."""
        if not source.rtree_table or not source.sqlite_uri:
            return None
        key = source.source_id
        if key not in self._summaries:
            try:
                self._summaries[key] = self._parse_node_summary(source)
            except (OSError, ValueError, sqlite3.Error):
                self._summaries[key] = None
        return self._summaries[key]

    def _parse_node_summary(self, source: VectorSource) -> tuple | None:
        import numpy as np

        connection = self._connection(source)
        node_table = _quote_identifier(f"{source.rtree_table}_node")
        row = connection.execute(
            f"SELECT data FROM {node_table} WHERE nodeno = 1"
        ).fetchone()
        if row is None:
            raise ValueError("the rtree has no root node")
        depth = int.from_bytes(row[0][:2], "big")
        if depth < 1:
            return None
        blobs = [row[0]]
        for level in range(depth):
            ids, bounds = [], []
            for blob in blobs:
                count = int.from_bytes(blob[2:4], "big")
                cells = np.frombuffer(
                    blob, dtype=np.uint8, count=count * 24, offset=4
                ).reshape(count, 24)
                ids.append(cells[:, :8].copy().view(">i8").ravel())
                bounds.append(cells[:, 8:].copy().view(">f4").reshape(count, 4))
            child_ids = np.concatenate(ids)
            child_bounds = np.concatenate(bounds).astype(np.float64)
            if level < depth - 1:
                blobs = self._fetch_nodes(connection, node_table, child_ids)
        if not (source.feature_count / 500 <= len(child_bounds) <= source.feature_count):
            raise ValueError("the rtree node walk is inconsistent with the layer")
        per_node = source.feature_count / len(child_bounds)
        return (
            child_bounds[:, 0],
            child_bounds[:, 1],
            child_bounds[:, 2],
            child_bounds[:, 3],
            per_node,
        )

    def _fetch_nodes(self, connection, node_table: str, ids) -> list[bytes]:
        ids = [int(value) for value in ids]
        blobs: list[bytes] = []
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            rows = connection.execute(
                f"SELECT data FROM {node_table} WHERE nodeno IN ({marks})",
                chunk,
            ).fetchall()
            if len(rows) != len(chunk):
                raise ValueError("the rtree node walk lost nodes")
            blobs.extend(row[0] for row in rows)
        return blobs

    def _exact_grid(
        self,
        source: VectorSource,
        bbox: tuple[float, float, float, float],
        size: int,
        cancel: threading.Event | None,
    ):
        import numpy as np

        span_x = bbox[2] - bbox[0]
        span_y = bbox[3] - bbox[1]
        table = _quote_identifier(source.rtree_table)
        rows = self._query(
            source,
            cancel,
            f"""
            SELECT
                min(
                    ? - 1,
                    max(
                        0,
                        cast(
                            ((((minx + maxx) * 0.5) - ?) / ? * ?)
                            AS INTEGER
                        )
                    )
                ) AS cell_x,
                min(
                    ? - 1,
                    max(
                        0,
                        cast(
                            ((((miny + maxy) * 0.5) - ?) / ? * ?)
                            AS INTEGER
                        )
                    )
                ) AS cell_y,
                count(*) AS feature_count
            FROM {table}
            WHERE maxx >= ? AND minx <= ? AND maxy >= ? AND miny <= ?
            GROUP BY cell_x, cell_y
            """,
            (
                size,
                bbox[0],
                span_x,
                size,
                size,
                bbox[1],
                span_y,
                size,
                bbox[0],
                bbox[2],
                bbox[1],
                bbox[3],
            ),
        )
        grid = np.zeros((size, size))
        for cell_x, cell_y, count in rows:
            grid[int(cell_x), int(cell_y)] = count
        return grid

    def _overview_tile(
        self,
        source: VectorSource,
        bbox: tuple[float, float, float, float],
        z: int,
        x: int,
        y: int,
        cancel: threading.Event | None = None,
    ) -> bytes:
        import numpy as np

        if not source.rtree_table or not source.sqlite_uri:
            return b""
        size = max(8, min(256, OVERVIEW_GRID_SIZE))
        span_x = bbox[2] - bbox[0]
        span_y = bbox[3] - bbox[1]
        if span_x <= 0 or span_y <= 0:
            return b""
        grid = None
        summary = self._node_summary(source)
        if summary is not None:
            minx, maxx, miny, maxy, per_node = summary
            inside = (
                (maxx >= bbox[0]) & (minx <= bbox[2])
                & (maxy >= bbox[1]) & (miny <= bbox[3])
            )
            if inside.sum() * per_node > OVERVIEW_EXACT_MAX:
                grid = _coverage_grid(
                    minx[inside], maxx[inside], miny[inside], maxy[inside],
                    per_node, bbox, size,
                )
        if grid is None:
            grid = self._exact_grid(source, bbox, size, cancel)

        cell_x, cell_y = np.nonzero(grid >= 0.5)
        if len(cell_x) == 0:
            return b""
        counts = np.rint(np.maximum(grid[cell_x, cell_y], 1)).astype(np.int64)
        cell_w = span_x / size
        cell_h = span_y / size
        x0 = bbox[0] + cell_x * cell_w
        y0 = bbox[1] + cell_y * cell_h
        corner_x = np.column_stack([x0, x0 + cell_w, x0 + cell_w, x0]).ravel()
        corner_y = np.column_stack([y0, y0, y0 + cell_h, y0 + cell_h]).ravel()
        if source.crs.upper() not in {"EPSG:4326", "OGC:CRS84"}:
            corner_x, corner_y = self._transformer(source).transform(
                corner_x, corner_y
            )
        tx, ty = _tile_units(corner_x, corner_y, z, x, y)
        quads = np.rint(
            np.stack([tx.reshape(-1, 4), ty.reshape(-1, 4)], axis=2)
        ).astype(np.int64)
        writer = LayerWriter("layer", MVT_EXTENT)
        for quad, count in zip(quads, counts):
            area = _shoelace2(quad)
            if area == 0:
                continue
            ring = quad if area > 0 else quad[::-1]
            commands: list[int] = []
            path_commands(commands, ring.tolist(), [0, 0], close=True)
            writer.feature(POLYGON, commands, {"feature_count": int(count)})
        return writer.tile()

    def _read_detail(
        self,
        source: VectorSource,
        bbox: tuple[float, float, float, float],
        fids: list[int] | None,
    ):
        import pyogrio

        kwargs: dict[str, Any] = {
            "columns": source.attributes,
        }
        if fids is not None:
            if not fids:
                return None
            kwargs["fids"] = fids
        else:
            kwargs["bbox"] = bbox
            kwargs["max_features"] = MAX_TILE_FEATURES + 1
        with _gdal_env():
            metadata, table = pyogrio.read_arrow(
                source.locator,
                layer=source.layer,
                **kwargs,
            )
        if table.num_rows == 0 or table.num_rows > MAX_TILE_FEATURES:
            return None
        return metadata, table

    def _detail_tile(
        self,
        source: VectorSource,
        metadata: dict[str, Any],
        table: Any,
        z: int,
        x: int,
        y: int,
    ) -> bytes:
        import numpy as np
        import shapely

        geometry_name = metadata.get("geometry_name") or source.geometry_column
        geometries = shapely.from_wkb(
            table.column(geometry_name).to_numpy(zero_copy_only=False)
        )
        coords = shapely.get_coordinates(geometries)
        lon, lat = coords[:, 0], coords[:, 1]
        if source.crs.upper() not in {"EPSG:4326", "OGC:CRS84"}:
            lon, lat = self._transformer(source).transform(lon, lat)
        tx, ty = _tile_units(lon, lat, z, x, y)
        geometries = shapely.set_coordinates(
            geometries, np.column_stack([tx, ty])
        )
        geometries = shapely.clip_by_rect(
            geometries,
            -MVT_BUFFER,
            -MVT_BUFFER,
            MVT_EXTENT + MVT_BUFFER,
            MVT_EXTENT + MVT_BUFFER,
        )
        geometries = shapely.simplify(
            geometries, SIMPLIFY_TOLERANCE, preserve_topology=False
        )
        columns = {
            name: table.column(name).to_pylist()
            for name in table.column_names
            if name != geometry_name
        }
        writer = LayerWriter("layer", MVT_EXTENT)
        for row, geometry in enumerate(geometries):
            if geometry is None or geometry.is_empty:
                continue
            properties = {name: values[row] for name, values in columns.items()}
            for geometry_type, commands in _mvt_features(geometry):
                writer.feature(geometry_type, commands, properties)
        return writer.tile()

    def _encode_tile(
        self,
        source: VectorSource,
        z: int,
        x: int,
        y: int,
        cancel: threading.Event | None = None,
    ) -> bytes:
        if cancel is not None and cancel.is_set():
            raise TileCancelled(f"vector tile {z}/{x}/{y}")
        bounds_4326 = _tile_bounds_4326(z, x, y)
        if not _intersects(bounds_4326, source.bounds):
            return b""
        source_bbox = self._source_bbox(
            source,
            _buffered_bounds(bounds_4326),
        )
        dense, fids = self._indexed_feature_ids(source, source_bbox, cancel)
        if dense:
            return self._overview_tile(source, source_bbox, z, x, y, cancel)
        if cancel is not None and cancel.is_set():
            raise TileCancelled(f"vector tile {z}/{x}/{y}")
        detail = self._read_detail(source, source_bbox, fids)
        if detail is None:
            return b""
        metadata, table = detail
        return self._detail_tile(source, metadata, table, z, x, y)

    def _disk_cache_path(
        self,
        key: tuple[str, int, int, int],
    ) -> Path | None:
        if self.cache_dir is None:
            return None
        source_id, z, x, y = key
        return self.cache_dir / source_id / str(z) / str(x) / f"{y}.pbf"

    def _read_cached(
        self,
        key: tuple[str, int, int, int],
    ) -> bytes | None:
        with self.lock:
            cached = self.tile_cache.get(key)
            if cached is not None:
                self.tile_cache.move_to_end(key)
                return cached
        path = self._disk_cache_path(key)
        if path is None or not path.is_file():
            return None
        try:
            tile = path.read_bytes()
        except OSError:
            return None
        self._remember(key, tile)
        return tile

    def _remember(
        self,
        key: tuple[str, int, int, int],
        tile: bytes,
    ) -> None:
        with self.lock:
            self.tile_cache[key] = tile
            self.tile_cache.move_to_end(key)
            while len(self.tile_cache) > MAX_TILE_CACHE:
                self.tile_cache.popitem(last=False)

    def _write_cached(
        self,
        key: tuple[str, int, int, int],
        tile: bytes,
    ) -> None:
        self._remember(key, tile)
        path = self._disk_cache_path(key)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(
            f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_bytes(tile)
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def tile(
        self,
        source_id: str,
        z: int,
        x: int,
        y: int,
        cancel: threading.Event | None = None,
    ) -> bytes | None:
        if z < 0 or z > 22 or x < 0 or y < 0 or x >= 1 << z or y >= 1 << z:
            return b""
        with self.lock:
            source = self.sources.get(source_id)
        if source is None:
            return None
        key = (source_id, z, x, y)
        cached = self._read_cached(key)
        if cached is not None:
            return cached

        with self.lock:
            event = self.inflight.get(key)
            owner = event is None
            if owner:
                event = threading.Event()
                self.inflight[key] = event
        if not owner:
            waited = 0.0
            while not event.wait(timeout=0.5):
                waited += 0.5
                if cancel is not None and cancel.is_set():
                    raise TileCancelled(f"vector tile {z}/{x}/{y}")
                if waited >= 120:
                    raise TimeoutError(
                        f"timed out waiting for vector tile {z}/{x}/{y}"
                    )
            return self.tile(source_id, z, x, y, cancel)

        try:
            try:
                tile = self._encode_tile(source, z, x, y, cancel)
            except sqlite3.OperationalError:
                # A progress-handler abort lands as OperationalError.
                if cancel is not None and cancel.is_set():
                    raise TileCancelled(f"vector tile {z}/{x}/{y}") from None
                raise
            self._write_cached(key, tile)
            return tile
        finally:
            with self.lock:
                self.inflight.pop(key, None)
                event.set()
