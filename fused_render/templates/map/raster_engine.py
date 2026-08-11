"""Range-first raster source registry and XYZ tile renderer for Map Viewer.

The browser gives the service both the display path and a range-capable
``/api/fs/raw`` URL.  Rasterio/GDAL opens the URL through ``/vsicurl/`` so a
mounted cloud object is never opened through the kernel mount.  Optimized
sources are tiled directly; non-pyramided sources remain usable at detailed
zooms while a local COG derivative is built in the background.
"""
from __future__ import annotations

import contextlib
import hashlib
import math
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
import warnings
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from geo_paths import (
    is_http_url,
    is_managed_mount,
    is_native_remote_path,
    is_remote_path,
    is_vsi_path,
    normalize_remote_path,
)
from optional_runtime import require
from raster_categories import classify_categories, resolve_render_mode


AUTO_OPTIMIZE_MAX_BYTES = int(
    os.environ.get("MAP_VIEWER_AUTO_OPTIMIZE_MAX_BYTES", str(512 << 20))
)
PREVIEW_MAX_SIZE = int(os.environ.get("MAP_VIEWER_PREVIEW_MAX_SIZE", "512"))
PREVIEW_VERSION = "v3"
MAX_TILE_CACHE = int(os.environ.get("MAP_VIEWER_TILE_CACHE_SIZE", "512"))
RASTER_SUFFIXES = {
    ".tif",
    ".tiff",
    ".cog",
    ".vrt",
    ".jp2",
    ".j2k",
    ".img",
    ".ntf",
    ".nitf",
    ".dem",
    ".dt0",
    ".dt1",
    ".dt2",
    ".hgt",
    ".grd",
    ".nc",
    ".hdf",
    ".h5",
}
RASTER_RUNTIME = {
    "numpy": "numpy",
    "rasterio": "rasterio",
    "rio_tiler": "rio-tiler",
}


def _raster_dependency_error() -> str | None:
    return require("Raster layers", RASTER_RUNTIME)


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
        "message": message,
        "detected_type": "raster",
    }


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if hasattr(value, "item"):
        return value.item()
    return value


def _raw_url(origin: str, path: str) -> str:
    return (
        origin.rstrip("/")
        + "/api/fs/raw?path="
        + quote(path, safe="")
        + "&pooled=1"
    )


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
        GDAL_NUM_THREADS="ALL_CPUS",
    ):
        yield


def _source_size(source: str) -> int | None:
    if is_http_url(source):
        try:
            request = urllib.request.Request(source, method="HEAD")
            with urllib.request.urlopen(request, timeout=15) as response:
                value = response.headers.get("Content-Length")
            return int(value) if value else None
        except Exception:
            return None
    try:
        return os.path.getsize(source)
    except OSError:
        return None


def _source_fingerprint(
    target: str,
    source: str,
    source_size: int | None,
    locator: str,
) -> str:
    identity = f"{target}|{source}"
    if not is_remote_path(source):
        identity += f"|{source_size}"
    if not is_vsi_path(locator) and os.path.isfile(locator):
        source_stat = os.stat(locator)
        identity += f"|{source_stat.st_size}|{source_stat.st_mtime_ns}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _ranges(data: Any) -> list[list[float]]:
    import numpy as np

    result: list[list[float]] = []
    for band in np.ma.asarray(data):
        values = band.compressed() if np.ma.isMaskedArray(band) else np.asarray(band).ravel()
        finite = values[np.isfinite(values)]
        if finite.size:
            lo, hi = np.percentile(finite, [2, 98])
            lo, hi = float(lo), float(hi)
            if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
                lo, hi = float(np.nanmin(finite)), float(np.nanmax(finite))
            if hi <= lo:
                hi = lo + 1.0
        else:
            lo, hi = 0.0, 1.0
        result.append([lo, hi])
    return result


def _infer_background(dataset: Any, indexes: list[int]) -> float | None:
    """Infer a zero-filled collar without treating valid zero data as nodata."""
    import numpy as np

    edge_points: list[tuple[int, int]] = []
    samples_per_edge = 12
    for index in range(samples_per_edge):
        column = round(index * (dataset.width - 1) / (samples_per_edge - 1))
        row = round(index * (dataset.height - 1) / (samples_per_edge - 1))
        edge_points.extend(
            [
                (0, column),
                (dataset.height - 1, column),
                (row, 0),
                (row, dataset.width - 1),
            ]
        )
    interior_points = [
        (round(row * dataset.height / 5), round(column * dataset.width / 5))
        for row in range(1, 5)
        for column in range(1, 5)
    ]

    def read(points: list[tuple[int, int]]) -> Any:
        coordinates = [dataset.xy(row, column) for row, column in points]
        return np.asarray(list(dataset.sample(coordinates, indexes=indexes)))

    edges = read(edge_points)
    interior = read(interior_points)
    edge_zero = np.all(edges == 0, axis=1)
    interior_nonzero = np.any(interior != 0, axis=1)
    if float(np.mean(edge_zero)) >= 0.9 and float(np.mean(interior_nonzero)) >= 0.25:
        return 0.0
    return None


def _dtype_ranges(dtypes: tuple[str, ...], bands: int) -> list[list[float]]:
    import numpy as np

    result: list[list[float]] = []
    for dtype in dtypes[:bands]:
        kind = np.dtype(dtype)
        if np.issubdtype(kind, np.integer):
            info = np.iinfo(kind)
            result.append([float(info.min), float(info.max)])
        else:
            result.append([0.0, 1.0])
    return result


@dataclass
class RasterSource:
    source_id: str
    target: str
    source: str
    locator: str
    driver: str
    width: int
    height: int
    count: int
    dtypes: tuple[str, ...]
    crs: str
    bounds: list[float]
    minzoom: int
    maxzoom: int
    block_shapes: list[list[int]]
    overviews: list[int]
    source_size: int | None
    layout: str | None
    nodata: float | None
    inferred_nodata: float | None
    colormap: str
    rescale: list[list[float]]
    auto_rescale: bool = True
    render_mode: str = "single"
    auto_render_mode: bool = True
    categories: list[dict[str, Any]] | None = None
    category_colors: dict[int, tuple[int, ...]] = field(default_factory=dict)
    preview_path: str | None = None
    optimized_path: str | None = None
    optimization: dict[str, Any] = field(
        default_factory=lambda: {"status": "not_needed", "progress": 100}
    )

    @property
    def active_locator(self) -> str:
        return self.optimized_path or self.locator

    @property
    def has_overviews(self) -> bool:
        return bool(self.overviews) or self.optimized_path is not None

    def locator_for_zoom(self, zoom: int) -> str:
        if self.optimized_path:
            return self.optimized_path
        if self.preview_path and zoom < self.minzoom:
            return self.preview_path
        return self.locator


class RasterEngine:
    def __init__(self, cache_dir: str, base_url: str, token: str):
        self.cache_dir = Path(cache_dir)
        self.optimized_dir = self.cache_dir / "optimized"
        self.optimized_dir.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.sources: dict[str, RasterSource] = {}
        self.upstreams: dict[str, tuple[str, str]] = {}
        self.lock = threading.RLock()
        self.tile_cache: OrderedDict[tuple[Any, ...], bytes] = OrderedDict()
        self._transparent: bytes | None = None

    def locator(self, source: str, target: str) -> str:
        source = normalize_remote_path(source)
        if is_vsi_path(source):
            return source
        if not is_http_url(source):
            return source

        # GDAL appends auxiliary-file suffixes to the URL it is given.  With
        # `/api/fs/raw?path=scene.ntf`, `.rv1` lands after the query string and
        # the endpoint serves scene.ntf again, recursively.  Give GDAL a real
        # filename path and relay its Range requests to the opaque source URL.
        source_key = hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
        filename = Path(target.replace("\\", "/")).name
        if not filename:
            filename = Path(urlsplit(source).path).name or "raster.bin"
        filename = filename.replace("/", "_").replace("\\", "_")
        with self.lock:
            self.upstreams[source_key] = (source, filename)
        return (
            f"/vsicurl/{self.base_url}/upstream/{quote(self.token, safe='')}/"
            f"{source_key}/{quote(filename, safe='._-')}"
        )

    def upstream(
        self,
        token: str,
        source_key: str,
        filename: str,
        method: str,
        range_header: str | None,
    ) -> tuple[int, dict[str, str], bytes] | None:
        if not secrets.compare_digest(token, self.token):
            return None
        with self.lock:
            registered = self.upstreams.get(source_key)
        if registered is None or filename != registered[1]:
            return None

        source = registered[0]
        headers = {}
        if range_header:
            headers["Range"] = range_header
        request = urllib.request.Request(source, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = b"" if method == "HEAD" else response.read()
                response_headers = {
                    name: value
                    for name in (
                        "Content-Type",
                        "Content-Length",
                        "Content-Range",
                        "Accept-Ranges",
                        "ETag",
                        "Last-Modified",
                    )
                    if (value := response.headers.get(name))
                }
                return response.status, response_headers, body
        except urllib.error.HTTPError as error:
            body = b"" if method == "HEAD" else error.read()
            return error.code, {"Content-Type": "application/octet-stream"}, body

    def try_describe(self, req: dict[str, Any], obj: Any | None = None):
        target = obj if isinstance(obj, (str, os.PathLike)) else req.get("target")
        if not isinstance(target, (str, os.PathLike)):
            return None
        target = str(target).strip()
        if is_remote_path(target):
            target = normalize_remote_path(target)
        if not target or target.lower().split("?")[0].endswith(".py"):
            return None

        suffix_target = (
            urlsplit(target).path
            if is_http_url(target)
            else target
        )
        suffix = Path(suffix_target.replace("\\", "/")).suffix.lower()
        if suffix in RASTER_SUFFIXES:
            dependency_error = _raster_dependency_error()
            if dependency_error:
                return _dependency_descriptor(
                    str(req.get("artifact_id") or ""),
                    dependency_error,
                )

        direct_target = str(req.get("target") or "")
        if is_remote_path(direct_target):
            direct_target = normalize_remote_path(direct_target)
        supplied_url = str(req.get("source_url") or "")
        if is_remote_path(supplied_url):
            supplied_url = normalize_remote_path(supplied_url)
        is_local_file = (
            not is_remote_path(target)
            and not is_managed_mount(target)
            and os.path.isfile(target)
        )
        if is_local_file:
            source = os.path.abspath(target)
        elif is_native_remote_path(target):
            source = target
        elif target == direct_target and supplied_url:
            source = supplied_url
        elif is_http_url(target):
            source = target
        else:
            origin = str(req.get("source_origin") or "")
            source = _raw_url(origin, target) if origin else os.path.abspath(target)

        try:
            return self._describe(
                target=target,
                source=source,
                artifact_id=str(req.get("artifact_id") or ""),
                opts=req.get("opts") or {},
            )
        except Exception as error:
            if suffix in RASTER_SUFFIXES:
                return {
                    "id": str(req.get("artifact_id") or ""),
                    "status": "error",
                    "kind": None,
                    "bounds": None,
                    "data": {},
                    "stats": {},
                    "style": {},
                    "warnings": [],
                    "message": (
                        "Raster metadata could not be read through the range "
                        f"transport: {type(error).__name__}: {error}"
                    ),
                    "detected_type": "raster",
                }
            return None

    def _describe(
        self, target: str, source: str, artifact_id: str, opts: dict[str, Any]
    ) -> dict[str, Any]:
        import numpy as np
        import rasterio
        from rasterio.warp import transform_bounds
        from rio_tiler.errors import NoOverviewWarning
        from rio_tiler.io import Reader

        locator = self.locator(source, target)
        with _gdal_env(), rasterio.open(locator) as dataset:
            if dataset.driver in {"OGR_VRT"}:
                raise ValueError("not a raster dataset")
            if dataset.crs is None:
                gcps, gcp_crs = dataset.gcps
                has_rpc = dataset.rpcs is not None
                message = "Raster has no directly usable CRS."
                if gcps and gcp_crs:
                    message += " It contains GCPs, but a normalized grid is required."
                elif has_rpc:
                    message += " It contains RPC metadata, but an elevation model is required."
                else:
                    message += " Assign a CRS/extent or provide a georeferencing sidecar."
                return {
                    "id": artifact_id,
                    "status": "not_georeferenced",
                    "kind": None,
                    "bounds": None,
                    "data": {},
                    "stats": {
                        "driver": dataset.driver,
                        "width": dataset.width,
                        "height": dataset.height,
                        "has_gcps": bool(gcps and gcp_crs),
                        "has_rpc": has_rpc,
                    },
                    "style": {},
                    "warnings": [],
                    "message": message,
                    "detected_type": f"{dataset.driver} raster",
                }

            bounds = [
                float(value)
                for value in transform_bounds(
                    dataset.crs, "EPSG:4326", *dataset.bounds, densify_pts=21
                )
            ]
            overviews = dataset.overviews(1) if dataset.count else []
            image_structure = dataset.tags(ns="IMAGE_STRUCTURE")
            block_shapes = [list(shape) for shape in dataset.block_shapes]
            count = dataset.count
            indexes = (1, 2, 3) if count >= 3 else 1
            index_list = [1, 2, 3] if count >= 3 else [1]
            render_bands = 3 if count >= 3 else 1
            inferred_nodata = (
                _infer_background(dataset, index_list)
                if dataset.nodata is None
                else None
            )
            requested = opts.get("rescale")
            auto_rescale = True
            sample_data = None
            if (
                isinstance(requested, list)
                and len(requested) == 2
                and all(isinstance(value, (int, float)) for value in requested)
            ):
                rescale = [[float(requested[0]), float(requested[1])]] * render_bands
                auto_rescale = False
            elif overviews:
                with Reader(locator) as reader:
                    preview = reader.preview(indexes=indexes, max_size=256)
                preview_data = preview.array
                if inferred_nodata is not None:
                    preview_data = preview_data.copy()
                    preview_data.mask = np.logical_or(
                        np.ma.getmaskarray(preview_data),
                        np.all(preview_data.data == inferred_nodata, axis=0)[None, :, :],
                    )
                rescale = _ranges(preview_data)
                sample_data = preview_data
            else:
                rescale = _dtype_ranges(dataset.dtypes, render_bands)

            # Categorical eligibility only makes sense for a single-band
            # source (an RGB composite isn't classified data). Reuses the
            # preview read above when there is one; a manual/optimized
            # rescale skips that read, so take one here instead purely to
            # sample which discrete values are present.
            categories = None
            category_colors: dict[int, tuple[int, ...]] = {}
            embedded_colormap = None
            if render_bands == 1:
                if sample_data is None:
                    try:
                        with Reader(locator) as reader:
                            fallback_preview = reader.preview(indexes=indexes, max_size=256)
                        sample_data = fallback_preview.array
                        if inferred_nodata is not None:
                            sample_data = sample_data.copy()
                            sample_data.mask = np.logical_or(
                                np.ma.getmaskarray(sample_data),
                                np.all(sample_data.data == inferred_nodata, axis=0)[None, :, :],
                            )
                    except Exception:
                        sample_data = None
                with contextlib.suppress(ValueError):
                    embedded_colormap = dataset.colormap(1)
                if sample_data is not None:
                    categories = classify_categories(
                        sample_data,
                        dataset.dtypes[0],
                        embedded_colormap=embedded_colormap,
                        overrides=opts.get("category_colors"),
                    )
                if categories:
                    category_colors = {c["value"]: tuple(c["color"]) for c in categories}
            requested_mode = str(opts.get("render_mode") or "")
            render_mode = resolve_render_mode(count, categories, embedded_colormap, requested_mode)

            with warnings.catch_warnings():
                warnings.simplefilter("ignore", NoOverviewWarning)
                with Reader(locator) as reader:
                    minzoom, maxzoom = int(reader.minzoom), int(reader.maxzoom)

            source_size = _source_size(source)
            fingerprint = _source_fingerprint(
                target, source, source_size, locator
            )
            record = RasterSource(
                source_id=fingerprint,
                target=target,
                source=source,
                locator=locator,
                driver=dataset.driver,
                width=dataset.width,
                height=dataset.height,
                count=count,
                dtypes=tuple(dataset.dtypes),
                crs=dataset.crs.to_string(),
                bounds=bounds,
                minzoom=minzoom,
                maxzoom=maxzoom,
                block_shapes=block_shapes,
                overviews=list(overviews),
                source_size=source_size,
                layout=image_structure.get("LAYOUT"),
                nodata=dataset.nodata,
                inferred_nodata=inferred_nodata,
                colormap=str(opts.get("colormap") or "viridis"),
                rescale=rescale,
                auto_rescale=auto_rescale,
                render_mode=render_mode,
                auto_render_mode=(requested_mode == ""),
                categories=categories,
                category_colors=category_colors,
            )

        derivative = self.optimized_dir / f"{fingerprint}.tif"
        preview_derivative = self.optimized_dir / (
            f"{fingerprint}.preview-{PREVIEW_VERSION}.tif"
        )
        if derivative.exists():
            try:
                with rasterio.open(derivative) as cached:
                    if cached.width == record.width and cached.height == record.height:
                        record.optimized_path = str(derivative)
                        record.overviews = cached.overviews(1)
                        record.optimization = {
                            "status": "ready",
                            "progress": 100,
                            "path": str(derivative),
                        }
            except Exception:
                pass
        if record.optimized_path is None and preview_derivative.exists():
            try:
                with rasterio.open(preview_derivative) as cached:
                    if cached.crs is not None and cached.count:
                        record.preview_path = str(preview_derivative)
                        if record.auto_rescale:
                            record.rescale = _ranges(cached.read(masked=True))
                        record.optimization = {
                            "status": "available",
                            "progress": 15,
                            "phase": "preview_ready",
                            "preview_ready": True,
                            "preview_path": str(preview_derivative),
                        }
            except Exception:
                pass

        notices = self._warnings(record)
        if not record.has_overviews and not record.preview_path:
            record.optimization = {"status": "available", "progress": 0}

        needs_preparation = not record.has_overviews and not record.preview_path
        auto_optimize = (
            needs_preparation
            and record.source_size is not None
            and record.source_size <= AUTO_OPTIMIZE_MAX_BYTES
        )
        with self.lock:
            existing = self.sources.get(fingerprint)
            if existing is not None:
                existing.colormap = record.colormap
                if not record.auto_render_mode or existing.auto_render_mode:
                    existing.render_mode = record.render_mode
                    existing.categories = record.categories
                    existing.category_colors = record.category_colors
                    existing.auto_render_mode = record.auto_render_mode
                if not record.auto_rescale:
                    existing.rescale = record.rescale
                    existing.auto_rescale = False
                if (
                    not existing.has_overviews
                    and not existing.preview_path
                    and existing.optimization.get("status") == "error"
                ):
                    self._start_preparation(
                        fingerprint, full_optimize=auto_optimize
                    )
                existing_notices = self._warnings(existing)
                if existing.optimization.get("status") in {"queued", "running"}:
                    existing_notices.append(
                        "The shared background optimization job is running."
                    )
            else:
                self.sources[fingerprint] = record
                if needs_preparation:
                    self._start_preparation(
                        fingerprint, full_optimize=auto_optimize
                    )

        if existing is not None:
            return self.descriptor(existing, artifact_id, existing_notices)

        if needs_preparation:
            notices.append(
                "A cached coarse preview is being prepared so the raster "
                "remains visible at low zoom."
            )
            if auto_optimize:
                notices.append(
                    "Full background optimization will continue because the "
                    "source is within the configured automatic conversion limit."
                )

        return self.descriptor(record, artifact_id, notices)

    @staticmethod
    def _warnings(record: RasterSource) -> list[str]:
        if not record.has_overviews:
            return [
                "This raster has no overview pyramid. Detailed tiles are read "
                "on demand; coarse zooms use a cached georeferenced preview."
            ]
        if record.layout == "COG" and record.optimized_path is None:
            return ["Cloud-optimized source: tiles use HTTP range reads."]
        return []

    def descriptor(
        self, record: RasterSource, artifact_id: str, warnings: list[str] | None = None
    ) -> dict[str, Any]:
        bands = 3 if record.count >= 3 else 1
        categorical = record.render_mode == "categorical"
        stats = [
            {
                "index": index + 1,
                "p2": None if categorical else record.rescale[index][0],
                "p98": None if categorical else record.rescale[index][1],
            }
            for index in range(min(bands, len(record.rescale)))
        ]
        token = quote(self.token, safe="")
        return {
            "id": artifact_id or record.source_id,
            "status": "ok",
            "kind": "raster_tiles",
            "crs_original": record.crs,
            "bounds": record.bounds,
            "data": {
                "source_id": record.source_id,
                "tile_url": (
                    f"{self.base_url}/tiles/{record.source_id}"
                    f"/{{z}}/{{x}}/{{y}}.png?t={token}"
                ),
                "job_url": (
                    f"{self.base_url}/jobs/{record.source_id}?t={token}"
                ),
                "optimize_url": (
                    f"{self.base_url}/optimize/{record.source_id}?t={token}"
                ),
            },
            "stats": {
                "driver": record.driver,
                "bands": record.count,
                "width": record.width,
                "height": record.height,
                "dtype": record.dtypes[0],
                "block_shapes": record.block_shapes,
                "overviews": record.overviews,
                "source_size": record.source_size,
                "layout": record.layout,
                "nodata": record.nodata,
                "inferred_nodata": record.inferred_nodata,
                "native_minzoom": record.minzoom,
                "band_stats": stats,
                "render_mode": record.render_mode,
                "categories": record.categories,
            },
            "style": {
                "opacity": 0.9,
                "colormap": record.colormap,
                "rescale": record.rescale[0] if record.count < 3 and not categorical else None,
                "render_mode": record.render_mode,
                "category_colors": {
                    str(value): list(color) for value, color in record.category_colors.items()
                },
            },
            "minzoom": 0,
            "maxzoom": record.maxzoom,
            "optimization": dict(record.optimization),
            "warnings": list(warnings or []),
            "message": None,
            "detected_type": f"{record.driver} raster",
        }

    def job(self, source_id: str) -> dict[str, Any] | None:
        with self.lock:
            record = self.sources.get(source_id)
            if record is None:
                return None
            return {
                "source_id": source_id,
                **dict(record.optimization),
                "minzoom": 0,
                "native_minzoom": record.minzoom,
                "maxzoom": record.maxzoom,
                "rescale": record.rescale,
            }

    def start_optimization(self, source_id: str) -> dict[str, Any] | None:
        return self._start_preparation(source_id, full_optimize=True)

    def _start_preparation(
        self, source_id: str, full_optimize: bool
    ) -> dict[str, Any] | None:
        with self.lock:
            record = self.sources.get(source_id)
            if record is None:
                return None
            status = record.optimization.get("status")
            if status == "ready":
                return dict(record.optimization)
            if status in {"queued", "running"}:
                if full_optimize:
                    record.optimization["full_requested"] = True
                return dict(record.optimization)
            record.optimization = {
                "status": "queued",
                "progress": 0,
                "phase": "optimize" if record.preview_path else "preview",
                "preview_ready": bool(record.preview_path),
                "preview_path": record.preview_path,
                "full_requested": full_optimize,
                "started_at": time.time(),
            }
        thread = threading.Thread(
            target=self._prepare, args=(source_id,), daemon=True
        )
        thread.start()
        return self.job(source_id)

    def _prepare(self, source_id: str) -> None:
        import numpy as np
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.transform import Affine
        from rasterio.vrt import WarpedVRT
        from rasterio.shutil import copy as rio_copy
        from rio_tiler.errors import NoOverviewWarning
        from rio_tiler.io import Reader

        with self.lock:
            record = self.sources[source_id]
            record.optimization.update(
                status="running",
                progress=5,
                phase="preview_read",
                phase_started_at=time.time(),
            )
            destination = self.optimized_dir / f"{source_id}.tif"
            temporary = self.optimized_dir / f"{source_id}.{os.getpid()}.tmp.tif"
            preview_destination = self.optimized_dir / (
                f"{source_id}.preview-{PREVIEW_VERSION}.tif"
            )
            preview_temporary = self.optimized_dir / (
                f"{source_id}.{os.getpid()}.preview.tmp.tif"
            )
            preview_stage = self.optimized_dir / (
                f"{source_id}.{os.getpid()}.preview.stage.tif"
            )
        try:
            if record.preview_path is None:
                preview_temporary.unlink(missing_ok=True)
                preview_stage.unlink(missing_ok=True)
                with _gdal_env(), rasterio.open(record.locator) as source:
                    scale = max(
                        1.0,
                        max(source.width, source.height) / PREVIEW_MAX_SIZE,
                    )
                    width = max(1, round(source.width / scale))
                    height = max(1, round(source.height / scale))
                    indexes = (
                        [1, 2, 3] if source.count >= 3 else [1]
                    )
                    transform = source.transform * Affine.scale(
                        source.width / width, source.height / height
                    )
                    render_nodata = (
                        record.nodata
                        if record.nodata is not None
                        else record.inferred_nodata
                    )
                    if record.inferred_nodata is not None:
                        with WarpedVRT(
                            source,
                            src_nodata=record.inferred_nodata,
                            nodata=record.inferred_nodata,
                            resampling=Resampling.average,
                        ) as overview_source:
                            data = overview_source.read(
                                indexes,
                                out_shape=(len(indexes), height, width),
                                resampling=Resampling.average,
                            )
                    else:
                        data = source.read(
                            indexes,
                            out_shape=(len(indexes), height, width),
                            resampling=Resampling.average,
                        )
                    with self.lock:
                        record.optimization.update(
                            progress=12,
                            phase="preview_write",
                            phase_started_at=time.time(),
                        )
                    with rasterio.open(
                        preview_stage,
                        "w",
                        driver="GTiff",
                        width=width,
                        height=height,
                        count=len(indexes),
                        dtype=data.dtype,
                        crs=source.crs,
                        transform=transform,
                        nodata=render_nodata,
                        tiled=True,
                        blockxsize=256,
                        blockysize=256,
                        compress="deflate",
                        BIGTIFF="IF_SAFER",
                    ) as preview:
                        preview.write(data)
                rio_copy(
                    preview_stage,
                    preview_temporary,
                    driver="COG",
                    BLOCKSIZE=256,
                    COMPRESS="DEFLATE",
                    OVERVIEWS="AUTO",
                    OVERVIEW_RESAMPLING="AVERAGE",
                )
                os.replace(preview_temporary, preview_destination)
                preview_stage.unlink(missing_ok=True)
                with self.lock:
                    record.preview_path = str(preview_destination)
                    if record.auto_rescale:
                        range_data = np.ma.asarray(data)
                        if render_nodata is not None:
                            range_data = np.ma.masked_equal(
                                range_data, render_nodata
                            )
                        record.rescale = _ranges(range_data)
                    record.optimization.update(
                        progress=15,
                        phase="preview_ready",
                        preview_ready=True,
                        preview_path=str(preview_destination),
                    )
                    self._drop_source_tiles(source_id)

            with self.lock:
                full_requested = bool(
                    record.optimization.get("full_requested")
                )
                if not full_requested:
                    record.optimization.update(
                        status="available",
                        progress=15,
                        phase="preview_ready",
                        finished_at=time.time(),
                    )
                    return
                record.optimization.update(
                    status="running", progress=20, phase="optimize"
                )

            temporary.unlink(missing_ok=True)
            with _gdal_env():
                rio_copy(
                    record.locator,
                    temporary,
                    driver="COG",
                    BLOCKSIZE=512,
                    COMPRESS="DEFLATE",
                    BIGTIFF="IF_SAFER",
                    OVERVIEWS="AUTO",
                    OVERVIEW_RESAMPLING="AVERAGE",
                )
            os.replace(temporary, destination)
            with rasterio.open(destination) as dataset:
                overviews = dataset.overviews(1)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", NoOverviewWarning)
                with Reader(str(destination)) as reader:
                    minzoom, maxzoom = int(reader.minzoom), int(reader.maxzoom)
            with self.lock:
                record.optimized_path = str(destination)
                record.overviews = list(overviews)
                record.minzoom, record.maxzoom = minzoom, maxzoom
                record.optimization = {
                    "status": "ready",
                    "progress": 100,
                    "phase": "ready",
                    "preview_ready": True,
                    "preview_path": record.preview_path,
                    "path": str(destination),
                    "finished_at": time.time(),
                }
                self._drop_source_tiles(source_id)
        except Exception as error:
            with self.lock:
                record.optimization = {
                    "status": "error",
                    "progress": 0,
                    "message": f"{type(error).__name__}: {error}",
                    "finished_at": time.time(),
                }
            try:
                temporary.unlink(missing_ok=True)
                preview_temporary.unlink(missing_ok=True)
                preview_stage.unlink(missing_ok=True)
            except OSError:
                pass

    def _drop_source_tiles(self, source_id: str) -> None:
        keys = [key for key in self.tile_cache if key[0] == source_id]
        for key in keys:
            self.tile_cache.pop(key, None)

    def transparent_tile(self) -> bytes:
        if self._transparent is None:
            import io
            from PIL import Image

            output = io.BytesIO()
            Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(output, "PNG")
            self._transparent = output.getvalue()
        return self._transparent

    def tile(self, source_id: str, z: int, x: int, y: int) -> bytes | None:
        import numpy as np
        from rio_tiler.colormap import cmap
        from rio_tiler.errors import NoOverviewWarning, TileOutsideBounds
        from rio_tiler.io import Reader

        with self.lock:
            record = self.sources.get(source_id)
            if record is None:
                return None
            if (
                not record.has_overviews
                and not record.preview_path
                and z < record.minzoom
            ):
                return self.transparent_tile()
            locator = record.locator_for_zoom(z)
            revision = locator
            categorical = record.render_mode == "categorical"
            key = (
                source_id, revision, z, x, y, record.colormap,
                tuple(map(tuple, record.rescale)), record.render_mode,
                tuple(sorted(record.category_colors.items())),
            )
            cached = self.tile_cache.get(key)
            if cached is not None:
                self.tile_cache.move_to_end(key)
                return cached
            indexes = (1, 2, 3) if record.count >= 3 else 1
            ranges = record.rescale
            colormap_name = record.colormap
            inferred_nodata = record.inferred_nodata
            category_colors = record.category_colors

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", NoOverviewWarning)
                with _gdal_env(), Reader(locator) as reader:
                    image = reader.tile(
                        x,
                        y,
                        z,
                        indexes=indexes,
                        # Categorical class codes must never blend into
                        # bogus intermediate values, including at the
                        # lower-resolution preview derivative.
                        resampling_method=(
                            "nearest"
                            if categorical or not (
                                record.preview_path and locator == record.preview_path
                            )
                            else "bilinear"
                        ),
                    )
            if inferred_nodata is not None:
                invalid = np.all(
                    image.array.data == inferred_nodata, axis=0
                )
                image.array.mask = np.logical_or(
                    np.ma.getmaskarray(image.array),
                    invalid[None, :, :],
                )
            if categorical and category_colors:
                png = image.render(img_format="PNG", colormap=category_colors)
            else:
                image.rescale(ranges)
                color = cmap.get(colormap_name) if record.count < 3 else None
                png = image.render(img_format="PNG", colormap=color)
        except TileOutsideBounds:
            png = self.transparent_tile()

        with self.lock:
            self.tile_cache[key] = png
            self.tile_cache.move_to_end(key)
            while len(self.tile_cache) > MAX_TILE_CACHE:
                self.tile_cache.popitem(last=False)
        return png
