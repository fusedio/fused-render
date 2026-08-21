"""Multidimensional (NetCDF/Zarr/HDF5) sources as XYZ raster tiles for Map Viewer.

The titiler-xarray recipe inside the existing map daemon: xarray opens the
store, one 2D slice per (variable, dim-selection) is prepared and cached, and
rio-tiler's ``XarrayReader`` cuts web-mercator tiles from it.  Each selection is
an immutable source with its own id, so scrubbing a time slider back and forth
hits the browser tile cache; colormap and rescale stay mutable on the record
exactly like ``RasterEngine``'s.  The engines are zarr, h5netcdf and scipy —
never netCDF4, whose bundled HDF5 clashes with h5py's in one process on Windows.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from geo_paths import (
    is_http_url,
    is_managed_mount,
    is_remote_path,
    multidim_suffix,
    normalize_remote_path,
    resolve_source,
    zarr_store,
)
from optional_runtime import require
from raster_engine import MAX_TILE_CACHE, band_ranges, error_descriptor, transparent_tile


MULTIDIM_RUNTIME = {
    "xarray": "xarray",
    "rioxarray": "rioxarray",
    "zarr": "zarr",
    "h5netcdf": "h5netcdf",
    "fsspec": "fsspec",
    "numpy": "numpy",
    "rio_tiler": "rio-tiler",
    "scipy": "scipy",
    "cftime": "cftime",
}
# Past this budget a slice stays lazy: slower per tile, but one huge
# selection cannot evict every other.
MAX_SLICE_CELLS = int(os.environ.get("MAP_VIEWER_MULTIDIM_SLICE_CELLS", str(32 << 20)))
MAX_OPEN_DATASETS = int(os.environ.get("MAP_VIEWER_MULTIDIM_DATASETS", "8"))
MAX_CACHED_SLICES = int(os.environ.get("MAP_VIEWER_MULTIDIM_SLICES", "8"))
MAX_SLICE_BYTES = int(os.environ.get("MAP_VIEWER_MULTIDIM_SLICE_BYTES", str(512 << 20)))
# A scrub mints one source per step, so the registry is bounded; an evicted
# id just re-describes.
MAX_SOURCES = int(os.environ.get("MAP_VIEWER_MULTIDIM_SOURCES", "256"))
# Native maxzoom is one cell per screen pixel — z2 for ERA5 — and stopping
# the source there leaves MapLibre stretching one blurry image over every
# closer view. Eight levels of headroom puts one cell at about a tile, which
# is as far in as there is anything to see.
OVERZOOM_LEVELS = 8
MAX_DIM_LABELS = 20000
MAX_DIMS_META = 32

_Y_NAMES = {"lat", "latitude", "y"}
_X_NAMES = {"lon", "longitude", "x"}


class Ungeoreferenced(ValueError):
    """The grid is readable but carries no usable CRS.

    The one failure GDAL is worth asking about: it reads georeferencing this
    engine cannot (subdataset transforms, projected NetCDF without CF
    grid_mapping), and when it cannot either, its `not_georeferenced` card
    tells the user the same thing in the words the rest of the app uses.
    Every other failure here is one this engine understands and GDAL does
    not, so its message must survive.
    """


def _spatial_dims(da: Any) -> tuple[str, str] | None:
    ydim = xdim = None
    for dim in map(str, da.dims):
        coord = da.coords.get(dim)
        attrs = coord.attrs if coord is not None else {}
        standard = str(attrs.get("standard_name", "")).lower()
        axis = str(attrs.get("axis", "")).upper()
        if ydim is None and (
            dim.lower() in _Y_NAMES
            or standard in {"latitude", "projection_y_coordinate"}
            or axis == "Y"
        ):
            ydim = dim
        elif xdim is None and (
            dim.lower() in _X_NAMES
            or standard in {"longitude", "projection_x_coordinate"}
            or axis == "X"
        ):
            xdim = dim
    if ydim is None or xdim is None:
        return None
    return ydim, xdim


def _require_rectilinear(da: Any, ydim: str, xdim: str) -> None:
    for coord in da.coords.values():
        if coord.ndim == 2 and {str(dim) for dim in coord.dims} == {ydim, xdim}:
            raise ValueError(
                "This variable sits on a curvilinear grid (2D latitude/longitude "
                "coordinates), which is not supported yet."
            )
    if ydim not in da.coords or xdim not in da.coords:
        raise Ungeoreferenced(
            f"Dimensions {ydim!r}/{xdim!r} carry no coordinate values, so the "
            "grid cannot be georeferenced."
        )


def _degree_direction(units: str) -> str:
    """Which compass direction a ``degree*`` unit names, "" when it names none."""
    tail = units[len("degree"):].lstrip("s").lstrip("_")
    for direction, initials in (("north", "n"), ("east", "e")):
        if tail == direction or tail == initials:
            return direction
    if tail in {"south", "s", "west", "w"}:
        return "reversed"
    return "" if not tail else "other"


def _looks_geographic(da: Any, ydim: str, xdim: str) -> bool:
    """Whether a CRS-less grid can be trusted to be EPSG:4326 degrees.

    ``_spatial_dims`` accepts bare ``y``/``x`` names and plain axis attrs,
    which say "this is the spatial pair" but nothing about units — a
    projected grid that lost its ``grid_mapping`` looks exactly like that,
    and stamping degrees on meters renders it at a nonsense location. Only a
    dim that declares itself geographic BY NAME or BY CF ATTRS, with values
    that fit in degrees, earns the assumption.
    """
    import numpy as np

    def declared(dim: str, names: set[str], standard: str, direction: str) -> bool:
        attrs = da.coords[dim].attrs
        units = str(attrs.get("units", "")).lower()
        return (
            dim.lower() in names
            or str(attrs.get("standard_name", "")).lower() == standard
            # Real files write degree_north, degrees_N, or just degrees; a
            # unit naming the OTHER direction is a mislabelled axis.
            or (units.startswith("degree") and _degree_direction(units) in {direction, ""})
        )

    if not declared(ydim, {"lat", "latitude"}, "latitude", "north"):
        return False
    if not declared(xdim, {"lon", "longitude"}, "longitude", "east"):
        return False
    y = da.coords[ydim].values
    x = da.coords[xdim].values
    return bool(
        y.size and x.size
        and np.abs(y).max() <= 90.5
        and x.min() >= -180.5
        and x.max() <= 360.5
    )


def _cell_size(da: Any) -> tuple[float, float] | None:
    """Grid spacing in CRS units, from the prepared slice's own coordinates."""
    import numpy as np

    steps = []
    for axis in ("x", "y"):
        values = da.coords[axis].values
        if values.size < 2:
            return None
        steps.append(float(np.abs(np.diff(values.astype("float64"))).mean()))
    return steps[0], steps[1]


def _sample_windows(da: Any, span: int = 512, spots: int = 3) -> Any:
    """Values from a few chunk-sized windows spread across a lazy slice."""
    import numpy as np

    height, width = int(da.sizes["y"]), int(da.sizes["x"])
    taken = []
    for row in range(spots):
        for column in range(spots):
            top = min(max(0, height * (2 * row + 1) // (2 * spots) - span // 2),
                      max(0, height - span))
            left = min(max(0, width * (2 * column + 1) // (2 * spots) - span // 2),
                       max(0, width - span))
            window = da.isel(
                y=slice(top, top + span), x=slice(left, left + span)
            ).values
            taken.append(np.asarray(window).ravel())
    return np.concatenate(taken)


def _spatial_variables(ds: Any) -> list[dict[str, Any]]:
    variables = []
    for name, da in ds.data_vars.items():
        if da.ndim < 2 or _spatial_dims(da) is None:
            continue
        variables.append(
            {
                "name": str(name),
                "dims": [str(dim) for dim in da.dims],
                "shape": [int(size) for size in da.shape],
                "dtype": str(da.dtype),
                "units": str(da.attrs.get("units", "")),
                "long_name": str(da.attrs.get("long_name", "")),
            }
        )
    return variables


def _resolve_sel(
    dims: list[dict[str, Any]], requested: dict[str, Any]
) -> dict[str, int]:
    sel = {}
    for dim in dims:
        name = str(dim["name"])
        size = int(dim["size"])
        if size == 0:
            raise ValueError(
                f"Dimension {name!r} is empty (size 0), so no slice can be "
                "selected."
            )
        try:
            index = int(requested.get(name, 0))
        except (TypeError, ValueError):
            index = 0
        sel[name] = min(max(index, 0), size - 1)
    return sel


def _dim_labels(coord: Any, units: str) -> tuple[list[str], bool]:
    import numpy as np

    if coord is None:
        return [], False
    values = coord.values
    sparse = values.size > MAX_DIM_LABELS
    if sparse:
        values = values[[0, -1]]
    if np.issubdtype(values.dtype, np.datetime64):
        labels = [str(label) for label in np.datetime_as_string(values, unit="m")]
    elif values.dtype.kind in "if":
        suffix = f" {units}" if units else ""
        labels = [f"{value:g}{suffix}" for value in values.tolist()]
    else:
        labels = [str(value) for value in values.tolist()]
    return labels, sparse


def _dim_metadata(
    da: Any, ydim: str, xdim: str, sel: dict[str, int]
) -> list[dict[str, Any]]:
    dims = []
    for dim in map(str, da.dims):
        if dim in (ydim, xdim):
            continue
        coord = da.coords.get(dim)
        units = str(coord.attrs.get("units", "")) if coord is not None else ""
        labels, sparse = _dim_labels(coord, units)
        dims.append(
            {
                "name": dim,
                "size": int(da.sizes[dim]),
                "index": sel.get(dim, 0),
                "units": units,
                "labels": labels,
                "sparse": sparse,
            }
        )
    return dims


def _store_identity(store: str) -> tuple[int, int] | None:
    """What must match for cached handles and slices to still be this file.

    A remote object has no cheap identity and is treated as immutable, like
    the raster path treats it. A ``.zarr`` directory's own mtime only changes
    on chunk add/remove, which is the best signal available without walking
    the store.
    """
    if is_remote_path(store):
        return None
    try:
        source_stat = os.stat(store)
    except OSError:
        return None
    return source_stat.st_size, source_stat.st_mtime_ns


def _source_fingerprint(
    target: str, store: str, variable: str, sel: dict[str, int]
) -> str:
    identity = f"{target}|{store}|{variable}|{json.dumps(sel, sort_keys=True)}"
    file_identity = _store_identity(store)
    if file_identity is not None:
        identity += f"|{file_identity[0]}|{file_identity[1]}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


@dataclass
class MultidimSource:
    source_id: str
    store: str
    suffix: str
    engine: str
    variable: str
    sel: dict[str, int]
    variables: list[dict[str, Any]]
    dims: list[dict[str, Any]]
    bounds: list[float]
    minzoom: int
    maxzoom: int
    native_maxzoom: int
    cell: tuple[float, float] | None
    width: int
    height: int
    dtype: str
    crs: str
    colormap: str
    rescale: list[list[float]]
    auto_rescale: bool = True


def _resolution_stats(record: MultidimSource) -> dict[str, Any] | None:
    """Cell size for the info line: CRS units, plus metres when degrees."""
    if record.cell is None:
        return None
    x, y = record.cell
    stats: dict[str, Any] = {"x": x, "y": y, "degrees": "4326" in record.crs}
    if stats["degrees"]:
        # Latitude step: longitude narrows with latitude and needs a location.
        stats["metres"] = y * 111320.0
    return stats


class MultidimEngine:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.sources: OrderedDict[str, MultidimSource] = OrderedDict()
        self.lock = threading.RLock()
        self.tile_cache: OrderedDict[tuple[Any, ...], bytes] = OrderedDict()
        # A build can be a multi-second remote read, so the lock is never
        # held across one: a pending Event marks it and the second asker
        # waits. Keys carry the file's identity, so a rewritten file misses.
        self.datasets: OrderedDict[tuple[Any, ...], tuple[str, Any]] = OrderedDict()
        self.slices: OrderedDict[tuple[Any, ...], tuple[str, Any]] = OrderedDict()
        self.data_lock = threading.RLock()
        # Bumped on eviction, so a slice built through the closed handle
        # cannot re-insert itself.
        self._store_gen: dict[str, int] = {}
        # A scrub asks for identical metadata every tick, and restringifying
        # 20k labels costs 2-15ms of it.
        self._dims_meta: OrderedDict[tuple[Any, ...], tuple[Any, Any]] = OrderedDict()

    def try_describe(self, req: dict[str, Any], obj: Any | None = None):
        target = obj if isinstance(obj, (str, os.PathLike)) else req.get("target")
        if not isinstance(target, (str, os.PathLike)):
            return None
        target = str(target).strip()
        if is_remote_path(target):
            target = normalize_remote_path(target)
        suffix = multidim_suffix(target)
        if not suffix:
            return None

        artifact_id = str(req.get("artifact_id") or "")
        dependency_error = require("Multidimensional datasets", MULTIDIM_RUNTIME)
        if dependency_error:
            return error_descriptor(
                artifact_id, dependency_error,
                detected_type="multidimensional dataset",
            )

        # resolve_source's isfile check would hand a local .zarr directory
        # the shell's range URL, which cannot serve a directory store.
        if (
            not is_remote_path(target)
            and not is_managed_mount(target)
            and os.path.isdir(target)
        ):
            source = os.path.abspath(target)
        else:
            source = resolve_source(req, target)

        try:
            return self._describe(
                target=target,
                source=source,
                suffix=suffix,
                artifact_id=artifact_id,
                opts=req.get("opts") or {},
            )
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            # This object may sit in a failure cache entry for its TTL, and
            # re-raising re-attached the frames below — datasets included.
            error.__traceback__ = None
            descriptor = error_descriptor(
                artifact_id, message,
                detected_type="multidimensional dataset",
            )
            descriptor["gdal_may_georeference"] = isinstance(error, Ungeoreferenced)
            return descriptor

    def _open(self, store: str, suffix: str) -> tuple[Any, str]:
        import xarray as xr

        if suffix == ".zarr":
            try:
                return (
                    xr.open_zarr(store, consolidated=True, decode_coords="all"),
                    "zarr",
                )
            except Exception:
                return (
                    xr.open_zarr(store, consolidated=False, decode_coords="all"),
                    "zarr",
                )

        def open_bytes(**kwargs: Any) -> Any:
            # Own byte source per attempt, closed on failure: a leaked fsspec
            # handle costs a connection, a local one locks the file.
            handle = self._byte_source(store)
            try:
                return xr.open_dataset(handle, decode_coords="all", **kwargs)
            except BaseException:
                if hasattr(handle, "close"):
                    with contextlib.suppress(Exception):
                        handle.close()
                raise

        if suffix in {".nc", ".nc4"}:
            try:
                return open_bytes(engine="h5netcdf"), "h5netcdf"
            except OSError as error:
                if "signature" not in str(error).lower():
                    raise
                # NetCDF3 has no HDF5 signature; scipy is its native reader.
                return open_bytes(engine="scipy"), "scipy"
        if suffix == ".hdf":
            try:
                return open_bytes(engine="h5netcdf"), "h5netcdf"
            except OSError as error:
                if "signature" not in str(error).lower():
                    raise
                raise ValueError(
                    "This is an HDF4 file. Reading HDF4 needs a GDAL build with "
                    "the HDF4 driver, which pip wheels do not ship — convert it "
                    "to NetCDF or HDF5 first."
                ) from error
        try:
            return open_bytes(engine="h5netcdf"), "h5netcdf"
        except Exception:
            # Plain HDF5 files often carry unlabeled dimensions that the netCDF
            # data model has no name for; phony_dims invents index names.
            return open_bytes(engine="h5netcdf", phony_dims="access"), "h5netcdf"

    @staticmethod
    def _byte_source(store: str) -> Any:
        if is_http_url(store):
            import fsspec

            return fsspec.open(
                store, "rb", cache_type="blockcache", block_size=1 << 20
            ).open()
        return store

    def _build_cached(
        self,
        cache: OrderedDict[tuple[Any, ...], tuple[str, Any]],
        key: tuple[Any, ...],
        limit: int,
        build: Callable[[], Any],
        evict: Callable[[tuple[Any, ...], Any], None] | None = None,
        cost: Callable[[Any], int] | None = None,
        budget: int = 0,
        keep: Callable[[], bool] | None = None,
    ) -> Any:
        while True:
            with self.data_lock:
                entry = cache.get(key)
                if entry is None:
                    pending = threading.Event()
                    cache[key] = ("pending", pending)
                    break
                state, value = entry
                if state == "ready":
                    cache.move_to_end(key)
                    return value
                if state == "failed":
                    # The whole burst fails at once rather than each waiter
                    # re-paying a slow open. A later request retries.
                    exception, failed_at = value
                    if time.monotonic() - failed_at < 5:
                        raise exception
                    cache.pop(key, None)
                    continue
            value.wait(timeout=600)
        try:
            built = build()
        except BaseException as error:
            # A cached traceback pins every frame it passed through.
            error.__traceback__ = None
            with self.data_lock:
                cache[key] = ("failed", (error, time.monotonic()))
            pending.set()
            raise
        try:
            with self.data_lock:
                if keep is not None and not keep():
                    # Stale: the caller still gets its handle, but the cache
                    # must not resurrect it. Pop only our own marker — an
                    # eviction may have dropped it and a second builder
                    # installed its own, which is still live work.
                    if cache.get(key) == ("pending", pending):
                        cache.pop(key, None)
                    return built
                cache[key] = ("ready", built)
                cache.move_to_end(key)
                now = time.monotonic()
                expired = [
                    k for k, (state, value) in cache.items()
                    if state == "failed" and now - value[1] >= 5
                ]
                for stale in expired:
                    cache.pop(stale, None)
                ready = [k for k, (state, _) in cache.items() if state == "ready"]
                over_budget = bool(
                    budget and cost
                    and sum(cost(cache[k][1]) for k in ready) > budget
                )
                while ready[:-1] and (len(ready) > limit or over_budget):
                    victim = ready.pop(0)
                    _, value = cache.pop(victim)
                    if evict is not None:
                        evict(victim, value)
                    if over_budget:
                        over_budget = sum(cost(cache[k][1]) for k in ready) > budget
        finally:
            pending.set()
        return built

    def _evict_dataset(self, key: tuple[Any, ...], value: tuple[Any, str]) -> None:
        """Close an evicted handle and drop the slices that read through it.

        Windows keeps the file locked for as long as the handle is open, and
        this daemon never exits — so an unclosed evicted handle would deny
        the file to its own author forever. A lazy slice mid-tile, or a
        describe mid-load, can lose the race and error once; the next
        request rebuilds both.
        """
        store = key[0]
        self._store_gen[store] = self._store_gen.get(store, 0) + 1
        stale = [k for k in self.slices if k[0] == store]
        for slice_key in stale:
            self.slices.pop(slice_key, None)
        with contextlib.suppress(Exception):
            value[0].close()

    def _open_dataset(self, store: str, suffix: str) -> tuple[Any, str]:
        identity = _store_identity(store)
        key = (store, identity)
        with self.data_lock:
            replaced = [
                k for k in self.datasets
                if k[0] == store and k != key and self.datasets[k][0] == "ready"
            ]
            for old_key in replaced:
                _, value = self.datasets.pop(old_key)
                self._evict_dataset(old_key, value)
        return self._build_cached(
            self.datasets,
            key,
            MAX_OPEN_DATASETS,
            lambda: self._open(store, suffix),
            evict=self._evict_dataset,
            keep=lambda: _store_identity(store) == identity,
        )

    def _prepare(self, ds: Any, variable: str, sel: dict[str, int]) -> Any:
        import numpy as np
        import rioxarray  # noqa: F401 — registers the .rio accessor

        da = ds[variable]
        spatial = _spatial_dims(da)
        if spatial is None:
            raise ValueError(f"Variable {variable!r} has no latitude/longitude dimensions.")
        ydim, xdim = spatial
        _require_rectilinear(da, ydim, xdim)
        geographic = _looks_geographic(da, ydim, xdim)
        if sel:
            da = da.isel(**sel)
        if da.ndim != 2:
            raise ValueError(
                f"Selection left {da.ndim} dimensions {tuple(map(str, da.dims))}; "
                "a renderable slice must be 2D."
            )
        if not geographic and da.rio.crs is None:
            # Before the read: a projected remote slice would otherwise
            # download in full and be thrown away.
            raise Ungeoreferenced(
                "The grid has no CRS and its coordinates do not look like "
                "degrees; the file needs CF grid_mapping metadata or a "
                "sidecar."
            )
        if da.size <= MAX_SLICE_CELLS:
            da = da.load()
            loaded = True
        else:
            loaded = False
        renames = {dim: name for dim, name in ((ydim, "y"), (xdim, "x")) if dim != name}
        if renames:
            da = da.rename(renames)
        da = da.transpose("y", "x")
        if da.rio.crs is None:
            da = da.rio.write_crs("EPSG:4326")
        # A 0-360 grid starts at or above zero. On `max > 180` alone this
        # also caught a -180..180 grid overhanging the meridian, and rolling
        # that one scrambles its columns.
        if (
            da.rio.crs.is_geographic
            and float(da.x.max()) > 180
            and float(da.x.min()) >= 0
        ):
            # In float64 whatever the axis dtype: rolling float32 longitudes
            # in their own precision leaves ~6e-5 of jitter, which the spacing
            # check below reads as a seam gap below 0.1deg.
            rolled = (da.x.values.astype("float64") + 180.0) % 360.0 - 180.0
            da = da.assign_coords(x=rolled).sortby("x")
            # A 0..360-inclusive grid ships the seam column twice, and the
            # duplicate x would read as a spacing jump below.
            x = da.x.values
            if x.size > 1:
                keep = np.concatenate(([True], np.diff(x) != 0))
                if not keep.all():
                    da = da.isel(x=np.flatnonzero(keep))
            # A regional grid crossing the seam (lon 150..210) rolls into two
            # blocks with a gap, which XarrayReader silently stretches across
            # the whole span.
            diffs = np.diff(da.x.values)
            if diffs.size and (
                diffs.max() - diffs.min() > max(abs(diffs.mean()) * 1e-3, 1e-9)
            ):
                raise ValueError(
                    "This grid crosses the antimeridian (longitudes span the "
                    "180 degree seam), which is not supported yet."
                )
        if da.rio.nodata is None and da.dtype.kind == "f":
            da = da.rio.write_nodata(float("nan"))
        # The seam dedup can drop a column, so size no longer answers "was
        # this materialized" and every later decision reads this instead.
        da.attrs["_fused_loaded"] = loaded
        return da

    def _slice(self, store: str, suffix: str, variable: str, sel: dict[str, int]) -> Any:
        key = (store, _store_identity(store), variable, tuple(sorted(sel.items())))
        # An eviction mid-build leaves the result readable but uncacheable:
        # the handle behind it is closed.
        generation = self._store_gen.get(store, 0)

        def build():
            ds, _ = self._open_dataset(store, suffix)
            return self._prepare(ds, variable, sel)

        def loaded_bytes(da: Any) -> int:
            # Only materialized slices hold memory; lazy ones cost window reads.
            return int(da.nbytes) if da.attrs.get("_fused_loaded") else 0

        return self._build_cached(
            self.slices,
            key,
            MAX_CACHED_SLICES,
            build,
            cost=loaded_bytes,
            budget=MAX_SLICE_BYTES,
            keep=lambda: self._store_gen.get(store, 0) == generation,
        )

    def _variable_meta(
        self, ds: Any, store: str, requested: str
    ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        """The variable to render, plus its variables list and dims metadata.

        A time scrub re-describes the same variable on every tick, and only
        the selected indexes differ; the label stringification (up to 20k
        labels, 2-15ms) is cached per (store, identity, variable) and each
        describe patches in its own indexes. The identity in the key retires
        the entry when the file is rewritten.
        """
        identity = _store_identity(store)
        if requested:
            with self.data_lock:
                cached = self._dims_meta.get((store, identity, requested))
                if cached is not None:
                    self._dims_meta.move_to_end((store, identity, requested))
                    return requested, cached[0], cached[1]
        variables = _spatial_variables(ds)
        if not variables:
            raise ValueError(
                "No mappable variables: every variable lacks recognizable "
                "latitude/longitude (or y/x) dimensions."
            )
        names = [entry["name"] for entry in variables]
        variable = requested or names[0]
        if variable not in names:
            raise ValueError(
                f"Variable {variable!r} not found. Available: {', '.join(names)}"
            )
        full = ds[variable]
        ydim, xdim = _spatial_dims(full)
        dims = _dim_metadata(full, ydim, xdim, {})
        with self.data_lock:
            self._dims_meta[(store, identity, variable)] = (variables, dims)
            self._dims_meta.move_to_end((store, identity, variable))
            while len(self._dims_meta) > MAX_DIMS_META:
                self._dims_meta.popitem(last=False)
        return variable, variables, dims

    def _describe(
        self, target: str, source: str, suffix: str, artifact_id: str, opts: dict[str, Any]
    ) -> dict[str, Any]:
        from rio_tiler.io.xarray import XarrayReader

        store = zarr_store(source) if suffix == ".zarr" else source
        ds, engine = self._open_dataset(store, suffix)
        variable, variables, dims_template = self._variable_meta(
            ds, store, str(opts.get("var") or "")
        )
        requested_sel = opts.get("sel")
        sel = _resolve_sel(
            dims_template,
            requested_sel if isinstance(requested_sel, dict) else {},
        )
        da = self._slice(store, suffix, variable, sel)
        lazy = not da.attrs.get("_fused_loaded")

        reader = XarrayReader(da)
        bounds = [float(value) for value in reader.get_geographic_bounds("EPSG:4326")]
        minzoom, maxzoom = int(reader.minzoom), int(reader.maxzoom)

        requested = opts.get("rescale")
        if (
            isinstance(requested, list)
            and len(requested) == 2
            and all(isinstance(value, (int, float)) for value in requested)
        ):
            rescale = [[float(requested[0]), float(requested[1])]]
            auto_rescale = False
        elif lazy:
            # A strided subsample would touch every chunk — the full download
            # the budget exists to avoid — so a few contiguous windows stand
            # in. Several, because one over a masked region is all nodata.
            rescale = band_ranges(_sample_windows(da)[None])
            auto_rescale = True
        else:
            step_y = max(1, da.sizes["y"] // 512)
            step_x = max(1, da.sizes["x"] // 512)
            rescale = band_ranges(da[::step_y, ::step_x].values[None])
            auto_rescale = True

        fingerprint = _source_fingerprint(target, store, variable, sel)
        record = MultidimSource(
            source_id=fingerprint,
            store=store,
            suffix=suffix,
            engine=engine,
            variable=variable,
            sel=sel,
            variables=variables,
            dims=[{**dim, "index": sel.get(dim["name"], 0)} for dim in dims_template],
            bounds=bounds,
            minzoom=minzoom,
            maxzoom=min(24, maxzoom + OVERZOOM_LEVELS),
            native_maxzoom=maxzoom,
            cell=_cell_size(da),
            width=int(da.sizes["x"]),
            height=int(da.sizes["y"]),
            dtype=str(da.dtype),
            crs=da.rio.crs.to_string(),
            colormap=str(opts.get("colormap") or "viridis"),
            rescale=rescale,
            auto_rescale=auto_rescale,
        )
        with self.lock:
            existing = self.sources.get(fingerprint)
            if existing is not None:
                existing.colormap = record.colormap
                if not record.auto_rescale or opts.get("stretch"):
                    existing.rescale = record.rescale
                    existing.auto_rescale = record.auto_rescale
                record = existing
                self.sources.move_to_end(fingerprint)
            else:
                self.sources[fingerprint] = record
                while len(self.sources) > max(MAX_SOURCES, 1):
                    # A scrub replaces its own history before a quieter
                    # layer's, and never the record this descriptor names.
                    others = [key for key in self.sources if key != fingerprint]
                    victim = next(
                        (
                            key for key in others
                            if self.sources[key].store == record.store
                            and self.sources[key].variable == record.variable
                        ),
                        others[0],
                    )
                    self.sources.pop(victim)

        warnings = []
        if lazy:
            warnings.append(
                "This slice is larger than the in-memory budget, so each tile "
                "reads its own window from the source."
            )
        return self.descriptor(record, artifact_id, warnings)

    def descriptor(
        self,
        record: MultidimSource,
        artifact_id: str,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
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
            },
            "stats": {
                "driver": record.engine,
                "bands": 1,
                "width": record.width,
                "height": record.height,
                "dtype": record.dtype,
                "nodata": None,
                "native_minzoom": record.minzoom,
                "native_maxzoom": record.native_maxzoom,
                "resolution": _resolution_stats(record),
                "band_stats": [
                    {
                        "index": 1,
                        "p2": record.rescale[0][0],
                        "p98": record.rescale[0][1],
                    }
                ],
                "render_mode": "single",
                "categories": None,
                "indexes": [1],
                "true_color": False,
                "multidim": {
                    "variable": record.variable,
                    "variables": record.variables,
                    "dims": record.dims,
                    "engine": record.engine,
                },
            },
            "style": {
                "opacity": 0.9,
                "colormap": record.colormap,
                "stretch": "auto",
                "rescale": record.rescale[0],
                "render_mode": "single",
                "category_colors": {},
            },
            "minzoom": 0,
            "maxzoom": record.maxzoom,
            "warnings": list(warnings or []),
            "message": None,
            "detected_type": f"{record.engine} multidimensional dataset",
        }

    def transparent_tile(self) -> bytes:
        return transparent_tile()

    def tile(self, source_id: str, z: int, x: int, y: int) -> bytes | None:
        from rio_tiler.colormap import cmap
        from rio_tiler.errors import TileOutsideBounds
        from rio_tiler.io.xarray import XarrayReader

        with self.lock:
            record = self.sources.get(source_id)
            if record is None:
                return None
            self.sources.move_to_end(source_id)
            key = (
                source_id, z, x, y, record.colormap,
                tuple(map(tuple, record.rescale)),
            )
            cached = self.tile_cache.get(key)
            if cached is not None:
                self.tile_cache.move_to_end(key)
                return cached
            colormap_name = record.colormap
            ranges = record.rescale

        try:
            da = self._slice(record.store, record.suffix, record.variable, record.sel)
            image = XarrayReader(da).tile(x, y, z)
            image.rescale(ranges)
            png = image.render(img_format="PNG", colormap=cmap.get(colormap_name))
        except TileOutsideBounds:
            png = self.transparent_tile()

        with self.lock:
            self.tile_cache[key] = png
            self.tile_cache.move_to_end(key)
            while len(self.tile_cache) > MAX_TILE_CACHE:
                self.tile_cache.popitem(last=False)
        return png
