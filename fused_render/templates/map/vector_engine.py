"""Bounded, on-demand vector tiles for Map Viewer.

Large vector files must never be serialized wholesale for the browser.  This
engine registers metadata in milliseconds, uses the source driver's spatial
filter for each visible XYZ tile, and encodes only a bounded feature sample as
Mapbox Vector Tiles.  GeoPackage sources with an RTree get an additional fast
path that selects feature IDs from the index before GDAL reads any geometry.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import math
import os
import sqlite3
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit


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
    os.environ.get("MAP_VIEWER_VECTOR_TILE_FEATURES", "2000")
)
MAX_TILE_CACHE = int(
    os.environ.get("MAP_VIEWER_VECTOR_TILE_CACHE_SIZE", "512")
)
MAX_ATTRIBUTES = int(
    os.environ.get("MAP_VIEWER_VECTOR_TILE_ATTRIBUTES", "8")
)
MVT_EXTENT = 4096
MVT_BUFFER = 64
WEB_MERCATOR_LIMIT = math.pi * 6378137.0
MAX_LATITUDE = 85.0511287798066


def _encoder_dependency_error() -> str | None:
    missing = [
        module
        for module in ("mapbox_vector_tile", "google.protobuf", "pyclipper")
        if importlib.util.find_spec(module) is None
    ]
    if not missing:
        return None
    return (
        "This Fused Render runtime is too old for streamed vector tiles "
        f"(missing {', '.join(missing)}). Install a build that includes the "
        "Map Viewer vector runtime; the template will not download packages "
        "while opening a map."
    )


def _dependency_descriptor(artifact_id: str, message: str) -> dict[str, Any]:
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
        "message": message,
    }


def _suffix(value: str) -> str:
    path = urlsplit(value).path if value.startswith(("http://", "https://")) else value
    return Path(path.replace("\\", "/")).suffix.lower()


def _raw_url(origin: str, path: str) -> str:
    return (
        origin.rstrip("/")
        + "/api/fs/raw?path="
        + quote(path, safe="")
        + "&pooled=1"
    )


def _resolve_source(request: dict[str, Any], target: str) -> str:
    supplied_url = str(request.get("source_url") or "")
    normalized = target.replace("\\", "/").lower()
    managed_mount = "/.fused-render/mounts/" in normalized
    local = (
        not target.startswith(("http://", "https://", "s3://", "/vsi"))
        and not managed_mount
        and os.path.isfile(target)
    )
    if local:
        return os.path.abspath(target)
    if target.startswith(("s3://", "/vsi")):
        return target
    if target == str(request.get("target") or "") and supplied_url:
        return supplied_url
    if target.startswith(("http://", "https://")):
        return target
    origin = str(request.get("source_origin") or "")
    return _raw_url(origin, target) if origin else os.path.abspath(target)


def _source_size(source: str) -> int | None:
    if source.startswith(("http://", "https://", "s3://", "/vsi")):
        return None
    try:
        return os.path.getsize(source)
    except OSError:
        return None


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


def _tile_bounds_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    span = 2.0 * WEB_MERCATOR_LIMIT / (1 << z)
    return (
        -WEB_MERCATOR_LIMIT + x * span,
        WEB_MERCATOR_LIMIT - (y + 1) * span,
        -WEB_MERCATOR_LIMIT + (x + 1) * span,
        WEB_MERCATOR_LIMIT - y * span,
    )


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


def _property(value: Any) -> bool | int | float | str | None:
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value
    return str(value)


@contextlib.contextmanager
def _gdal_env():
    import rasterio

    with rasterio.Env(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_USE_HEAD="YES",
        GDAL_HTTP_MULTIRANGE="YES",
        GDAL_HTTP_MAX_RETRY="2",
        GDAL_HTTP_RETRY_DELAY="0.2",
        CPL_VSIL_CURL_CHUNK_SIZE=str(64 << 10),
        CPL_VSIL_CURL_CACHE_SIZE=str(16 << 20),
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
    sqlite_connection: sqlite3.Connection | None = None
    sqlite_lock: threading.RLock | None = None


class VectorEngine:
    def __init__(
        self,
        base_url: str,
        token: str,
        locator: Callable[[str, str], str],
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.locator = locator
        self.sources: dict[str, VectorSource] = {}
        self.lock = threading.RLock()
        self.tile_cache: OrderedDict[tuple[str, int, int, int], bytes] = (
            OrderedDict()
        )

    def try_describe(self, request: dict[str, Any], obj: Any | None = None):
        target = obj if isinstance(obj, (str, os.PathLike)) else request.get("target")
        if not isinstance(target, (str, os.PathLike)):
            return None
        target = str(target).strip()
        if not target or _suffix(target) not in VECTOR_SUFFIXES:
            return None

        source = _resolve_source(request, target)
        source_size = _source_size(source)
        artifact_id = str(request.get("artifact_id") or "")
        if source_size is None or source_size >= VECTOR_TILE_MIN_BYTES:
            dependency_error = _encoder_dependency_error()
            if dependency_error:
                return _dependency_descriptor(artifact_id, dependency_error)
        try:
            locator = self.locator(source, target)
            return self._describe(
                target=target,
                locator=locator,
                artifact_id=artifact_id,
                source_size=source_size,
            )
        except Exception as error:
            if (
                source_size is not None
                and source_size < VECTOR_TILE_MIN_BYTES
            ):
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
        # Unknown remote sizes must stay on the bounded service path. Treating
        # "size unavailable" as "small" would fall back to GeoJSON and could
        # download or serialize an arbitrarily large cloud object.
        if (
            source_size is not None
            and source_size < VECTOR_TILE_MIN_BYTES
            and feature_count < VECTOR_TILE_MIN_FEATURES
        ):
            return None
        dependency_error = _encoder_dependency_error()
        if dependency_error:
            return _dependency_descriptor(artifact_id, dependency_error)

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
        fields_value = info.get("fields")
        dtypes_value = info.get("dtypes")
        fields = [
            str(value) for value in ([] if fields_value is None else fields_value)
        ]
        dtypes = [
            str(value) for value in ([] if dtypes_value is None else dtypes_value)
        ]
        columns = dict(zip(fields, dtypes))
        attributes = fields[:MAX_ATTRIBUTES]
        geometry_column = str(info.get("geometry_name") or "geometry")
        source_id = hashlib.sha256(
            f"{artifact_id}\0{locator}\0{layer}".encode("utf-8")
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
            existing = self.sources.get(source_id)
            if existing is None:
                self.sources[source_id] = source
            else:
                source = existing

        warnings = [
            (
                f"{feature_count:,} features are streamed as bounded vector "
                f"tiles (at most {MAX_TILE_FEATURES:,} source features per tile)."
            )
        ]
        if source.rtree_table:
            warnings.append(
                "GeoPackage RTree detected; visible tiles use indexed spatial reads."
            )
        else:
            warnings.append(
                "No directly queryable GeoPackage RTree was detected. The "
                "driver's spatial filter is still bounded, but unindexed "
                "sources can be slower."
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
        connection = None
        try:
            uri = f"file:{Path(source.locator).resolve().as_posix()}?mode=ro"
            connection = sqlite3.connect(
                uri,
                uri=True,
                check_same_thread=False,
                timeout=30,
            )
            row = connection.execute(
                "SELECT column_name FROM gpkg_geometry_columns "
                "WHERE table_name = ?",
                (source.layer,),
            ).fetchone()
            if not row:
                connection.close()
                return
            rtree = f"rtree_{source.layer}_{row[0]}"
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (rtree,),
            ).fetchone()
            if not exists:
                connection.close()
                return
            source.rtree_table = rtree
            source.sqlite_connection = connection
            source.sqlite_lock = threading.RLock()
        except (OSError, sqlite3.Error):
            if connection is not None:
                connection.close()

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

    def _indexed_fids(
        self,
        source: VectorSource,
        bbox: tuple[float, float, float, float],
    ) -> list[int] | None:
        connection = source.sqlite_connection
        lock = source.sqlite_lock
        if not source.rtree_table or connection is None or lock is None:
            return None
        table = _quote_identifier(source.rtree_table)
        spatial = (
            f"maxx >= ? AND minx <= ? AND maxy >= ? AND miny <= ?"
        )
        parameters = (bbox[0], bbox[2], bbox[1], bbox[3])
        with lock:
            count = int(
                connection.execute(
                    f"SELECT count(*) FROM {table} WHERE {spatial}",
                    parameters,
                ).fetchone()[0]
            )
            if count <= MAX_TILE_FEATURES:
                rows = connection.execute(
                    f"SELECT id FROM {table} WHERE {spatial}",
                    parameters,
                )
            else:
                stride = max(1, math.ceil(count / MAX_TILE_FEATURES))
                rows = connection.execute(
                    f"SELECT id FROM {table} WHERE {spatial} "
                    "AND id % ? = 0 LIMIT ?",
                    parameters + (stride, MAX_TILE_FEATURES),
                )
            return [int(row[0]) for row in rows]

    def _read_tile_frame(
        self,
        source: VectorSource,
        source_bbox: tuple[float, float, float, float],
    ):
        import pyogrio

        fids = self._indexed_fids(source, source_bbox)
        kwargs: dict[str, Any] = {
            "columns": source.attributes,
            "use_arrow": True,
            "on_invalid": "fix",
            "fid_as_index": True,
        }
        if fids is not None:
            if not fids:
                return None
            kwargs["fids"] = fids
        else:
            kwargs["bbox"] = source_bbox
            kwargs["max_features"] = MAX_TILE_FEATURES
        with _gdal_env():
            return pyogrio.read_dataframe(
                source.locator,
                layer=source.layer,
                **kwargs,
            )

    def _encode_tile(
        self,
        source: VectorSource,
        z: int,
        x: int,
        y: int,
    ) -> bytes:
        import mapbox_vector_tile
        import shapely

        bounds_4326 = _tile_bounds_4326(z, x, y)
        if not _intersects(bounds_4326, source.bounds):
            return b""
        mercator_bounds = _tile_bounds_3857(z, x, y)
        span = mercator_bounds[2] - mercator_bounds[0]
        buffer_mercator = span * MVT_BUFFER / MVT_EXTENT
        buffered_mercator = (
            mercator_bounds[0] - buffer_mercator,
            mercator_bounds[1] - buffer_mercator,
            mercator_bounds[2] + buffer_mercator,
            mercator_bounds[3] + buffer_mercator,
        )
        buffer_lon = (bounds_4326[2] - bounds_4326[0]) * MVT_BUFFER / MVT_EXTENT
        buffer_lat = (bounds_4326[3] - bounds_4326[1]) * MVT_BUFFER / MVT_EXTENT
        buffered_4326 = (
            max(-180.0, bounds_4326[0] - buffer_lon),
            max(-MAX_LATITUDE, bounds_4326[1] - buffer_lat),
            min(180.0, bounds_4326[2] + buffer_lon),
            min(MAX_LATITUDE, bounds_4326[3] + buffer_lat),
        )
        frame = self._read_tile_frame(
            source, self._source_bbox(source, buffered_4326)
        )
        if frame is None or frame.empty:
            return b""
        frame = frame[frame.geometry.notna() & ~frame.geometry.is_empty]
        if frame.empty:
            return b""
        if frame.crs is None:
            frame = frame.set_crs(source.crs)
        if frame.crs.to_epsg() != 3857:
            frame = frame.to_crs("EPSG:3857")

        geometries = frame.geometry.values
        valid = shapely.is_valid(geometries)
        if not bool(valid.all()):
            geometries = geometries.copy()
            geometries[~valid] = shapely.make_valid(geometries[~valid])
        tolerance = span / MVT_EXTENT * 1.5
        geometries = shapely.simplify(
            geometries,
            tolerance,
            preserve_topology=True,
        )
        geometries = shapely.clip_by_rect(
            geometries,
            *buffered_mercator,
        )

        features = []
        property_columns = [
            column for column in source.attributes if column in frame.columns
        ]
        for position, (fid, geometry) in enumerate(zip(frame.index, geometries)):
            if geometry is None or geometry.is_empty:
                continue
            properties = {}
            row = frame.iloc[position]
            for column in property_columns:
                value = _property(row[column])
                if value is not None:
                    properties[column] = value
            try:
                feature_id = int(fid)
            except (TypeError, ValueError):
                feature_id = position
            features.append(
                {
                    "id": feature_id,
                    "geometry": geometry,
                    "properties": properties,
                }
            )
        if not features:
            return b""
        return mapbox_vector_tile.encode(
            {"name": "layer", "features": features},
            default_options={
                "quantize_bounds": mercator_bounds,
                "extents": MVT_EXTENT,
                "y_coord_down": False,
                "check_winding_order": True,
                "on_invalid_geometry": (
                    mapbox_vector_tile.encoder.on_invalid_geometry_make_valid
                ),
            },
        )

    def tile(self, source_id: str, z: int, x: int, y: int) -> bytes | None:
        if z < 0 or z > 22 or x < 0 or y < 0 or x >= 1 << z or y >= 1 << z:
            return b""
        with self.lock:
            source = self.sources.get(source_id)
            if source is None:
                return None
            key = (source_id, z, x, y)
            cached = self.tile_cache.get(key)
            if cached is not None:
                self.tile_cache.move_to_end(key)
                return cached
        tile = self._encode_tile(source, z, x, y)
        with self.lock:
            self.tile_cache[key] = tile
            self.tile_cache.move_to_end(key)
            while len(self.tile_cache) > MAX_TILE_CACHE:
                self.tile_cache.popitem(last=False)
        return tile
