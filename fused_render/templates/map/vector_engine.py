"""Native, bounded vector tiles for Map Viewer.

Large vectors are never serialized wholesale for the browser. GeoPackage
sources use their RTree for bounded reads. Detailed tiles are encoded by
GDAL's native MVT writer, while tiles that contain more geometry than a screen
can distinguish become occupancy overviews instead of arbitrary feature
samples. Generated tiles are cached in memory and on disk.
"""
from __future__ import annotations

import contextlib
import gzip
import hashlib
import math
import os
import sqlite3
import tempfile
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

from geo_paths import (
    is_http_url,
    is_managed_mount,
    is_native_remote_path,
    is_remote_path,
    normalize_remote_path,
)
from optional_runtime import require


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
ENGINE_VERSION = "native-mvt-v1"
# The MVT tile pyramid's {z}/{x}/{y} math is fixed to EPSG:4326 — this is this
# pipeline's "canvas CRS" (the same role QGIS's project CRS plays: every layer,
# regardless of its own native CRS, is reprojected on-the-fly for that one
# render/tile). The source file's CRS is never touched. Previously left
# implicit, relying on GDAL's OGR MVT writer to silently reproject internally
# (GDAL >= 3.4 behavior) — now made explicit.
TILE_CRS = "EPSG:4326"


VECTOR_RUNTIME = {
    "geopandas": "geopandas",
    "pyarrow": "pyarrow",
    "pyogrio": "pyogrio",
    "pyproj": "pyproj",
    "rasterio": "rasterio",
    "shapely": "shapely",
}


def _vector_dependency_error() -> str | None:
    return require("Streamed vector layers", VECTOR_RUNTIME)


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
    path = urlsplit(value).path if is_http_url(value) else value
    return Path(path.replace("\\", "/")).suffix.lower()


def _raw_url(origin: str, path: str) -> str:
    return (
        origin.rstrip("/")
        + "/api/fs/raw?path="
        + quote(path, safe="")
        + "&pooled=1"
    )


def _resolve_source(request: dict[str, Any], target: str) -> str:
    target = normalize_remote_path(target) if is_remote_path(target) else target
    supplied_url = str(request.get("source_url") or "")
    if is_remote_path(supplied_url):
        supplied_url = normalize_remote_path(supplied_url)
    direct_target = str(request.get("target") or "")
    if is_remote_path(direct_target):
        direct_target = normalize_remote_path(direct_target)
    local = (
        not is_remote_path(target)
        and not is_managed_mount(target)
        and os.path.isfile(target)
    )
    if local:
        return os.path.abspath(target)
    if is_native_remote_path(target):
        return target
    if target == direct_target and supplied_url:
        return supplied_url
    if is_http_url(target):
        return target
    origin = str(request.get("source_origin") or "")
    return _raw_url(origin, target) if origin else os.path.abspath(target)


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


def _reproject_wkb(table: Any, geometry_name: str, source_crs: str) -> Any:
    import pyarrow as pa
    import shapely
    from pyproj import Transformer

    transformer = Transformer.from_crs(source_crs, TILE_CRS, always_xy=True)

    def _project(coordinates):
        projected = coordinates.copy()
        projected[:, 0], projected[:, 1] = transformer.transform(
            coordinates[:, 0], coordinates[:, 1]
        )
        return projected

    index = table.schema.get_field_index(geometry_name)
    geometries = shapely.from_wkb(
        table.column(index).to_numpy(zero_copy_only=False)
    )
    projected = shapely.to_wkb(shapely.transform(geometries, _project))
    return table.set_column(
        index,
        pa.field(geometry_name, pa.binary()),
        pa.array(projected, type=pa.binary()),
    )


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
        self.encode_slots = threading.BoundedSemaphore(2)
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

        source = _resolve_source(request, target)
        source_size = _source_size(source)
        artifact_id = str(request.get("artifact_id") or "")
        if source_size is None or source_size >= VECTOR_TILE_MIN_BYTES:
            dependency_error = _vector_dependency_error()
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
            return _dependency_descriptor(artifact_id, dependency_error)
        if "w" not in str(pyogrio.list_drivers().get("MVT", "")):
            raise RuntimeError(
                "the installed GDAL runtime does not provide the MVT writer"
            )

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
    ) -> tuple[bool, list[int] | None]:
        if not source.rtree_table or not source.sqlite_uri:
            return False, None
        table = _quote_identifier(source.rtree_table)
        with sqlite3.connect(
            source.sqlite_uri,
            uri=True,
            timeout=30,
        ) as connection:
            rows = connection.execute(
                f"SELECT id FROM {table} "
                "WHERE maxx >= ? AND minx <= ? AND maxy >= ? AND miny <= ? "
                "LIMIT ?",
                (bbox[0], bbox[2], bbox[1], bbox[3], MAX_TILE_FEATURES + 1),
            ).fetchall()
        dense = len(rows) > MAX_TILE_FEATURES
        return dense, None if dense else [int(row[0]) for row in rows]

    def _read_detail_arrow(
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
                return None, None
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
        if table.num_rows > MAX_TILE_FEATURES:
            return None, None
        if source.crs.upper() not in {"EPSG:4326", "OGC:CRS84"}:
            table = _reproject_wkb(
                table, metadata["geometry_name"], metadata["crs"]
            )
            metadata["crs"] = TILE_CRS
        return metadata, table

    def _overview_frame(
        self,
        source: VectorSource,
        bbox: tuple[float, float, float, float],
    ):
        if not source.rtree_table or not source.sqlite_uri:
            return None
        import geopandas as gpd
        from shapely.geometry import box

        size = max(8, min(256, OVERVIEW_GRID_SIZE))
        span_x = bbox[2] - bbox[0]
        span_y = bbox[3] - bbox[1]
        if span_x <= 0 or span_y <= 0:
            return None
        table = _quote_identifier(source.rtree_table)
        with sqlite3.connect(
            source.sqlite_uri,
            uri=True,
            timeout=30,
        ) as connection:
            rows = connection.execute(
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
            ).fetchall()
        if not rows:
            return None

        cell_width = span_x / size
        cell_height = span_y / size
        geometries = [
            box(
                bbox[0] + int(cell_x) * cell_width,
                bbox[1] + int(cell_y) * cell_height,
                bbox[0] + (int(cell_x) + 1) * cell_width,
                bbox[1] + (int(cell_y) + 1) * cell_height,
            )
            for cell_x, cell_y, _count in rows
        ]
        return gpd.GeoDataFrame(
            {"feature_count": [int(row[2]) for row in rows]},
            geometry=geometries,
            crs=source.crs,
        ).to_crs(TILE_CRS)

    def _native_tile(
        self,
        source: VectorSource,
        z: int,
        x: int,
        y: int,
        *,
        metadata: dict[str, Any] | None = None,
        arrow_table: Any | None = None,
    ) -> bytes:
        import pyogrio

        work_parent = self.cache_dir
        if work_parent is not None:
            work_parent.mkdir(parents=True, exist_ok=True)
        with self.encode_slots, tempfile.TemporaryDirectory(
            prefix="encode-",
            dir=str(work_parent) if work_parent is not None else None,
        ) as temporary:
            output = Path(temporary) / "tiles"
            dataset_options = {
                "MINZOOM": str(z),
                "MAXZOOM": str(z),
                "SIMPLIFICATION": "8",
                "SIMPLIFICATION_MAX_ZOOM": "4",
                "MAX_SIZE": "250000",
                "MAX_FEATURES": str(max(50000, MAX_TILE_FEATURES)),
            }
            layer_options = {
                "MINZOOM": str(z),
                "MAXZOOM": str(z),
                "NAME": "layer",
            }
            if arrow_table is not None and metadata is not None:
                with _gdal_env():
                    pyogrio.write_arrow(
                        arrow_table,
                        output,
                        layer="layer",
                        driver="MVT",
                        geometry_name=metadata["geometry_name"],
                        geometry_type=metadata["geometry_type"],
                        crs=TILE_CRS,
                        dataset_options=dataset_options,
                        layer_options=layer_options,
                    )
            else:
                return b""

            tile_path = output / str(z) / str(x) / f"{y}.pbf"
            if not tile_path.is_file():
                return b""
            tile = tile_path.read_bytes()
            return gzip.decompress(tile) if tile.startswith(b"\x1f\x8b") else tile

    def _encode_tile(
        self,
        source: VectorSource,
        z: int,
        x: int,
        y: int,
    ) -> bytes:
        bounds_4326 = _tile_bounds_4326(z, x, y)
        if not _intersects(bounds_4326, source.bounds):
            return b""
        source_bbox = self._source_bbox(
            source,
            _buffered_bounds(bounds_4326),
        )
        dense, fids = self._indexed_feature_ids(source, source_bbox)
        if dense:
            overview = self._overview_frame(source, source_bbox)
            if overview is None or overview.empty:
                return b""
            return self._native_tile(
                source,
                z,
                x,
                y,
                metadata={
                    "geometry_name": overview.geometry.name,
                    "geometry_type": "Polygon",
                    "crs": TILE_CRS,
                },
                arrow_table=overview.to_arrow(
                    index=False,
                    geometry_encoding="WKB",
                ),
            )

        metadata, table = self._read_detail_arrow(source, source_bbox, fids)
        if table is None or table.num_rows == 0:
            return b""
        return self._native_tile(
            source,
            z,
            x,
            y,
            metadata=metadata,
            arrow_table=table,
        )

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

    def tile(self, source_id: str, z: int, x: int, y: int) -> bytes | None:
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
            if not event.wait(timeout=120):
                raise TimeoutError(
                    f"timed out waiting for vector tile {z}/{x}/{y}"
                )
            return self.tile(source_id, z, x, y)

        try:
            tile = self._encode_tile(source, z, x, y)
            self._write_cached(key, tile)
            return tile
        finally:
            if owner:
                with self.lock:
                    self.inflight.pop(key, None)
                    event.set()
