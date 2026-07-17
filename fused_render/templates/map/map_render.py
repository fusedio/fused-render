"""runPython target for the map template: classify a geospatial file into a
map-layer descriptor (see geo_classify.py).

Raster and PMTiles targets are rendered to cached artifacts under
~/.fused-render/cache/map/<hash>/ keyed by (target, mtime, opts), so re-opening
an unchanged file is instant. Vector targets are served as on-the-fly Mapbox
Vector Tiles by a warm DuckDB daemon (vector_tile_server.py) — this call only
ensures the daemon is up, kicks off its per-file warm-up, and returns the tile
endpoint descriptor; the client polls the daemon's /status until ready.
"""

import hashlib
import json
import os

CACHE_ROOT = os.path.expanduser("~/.fused-render/cache/map")
_URLISH = ("http://", "https://", "s3://", "/vsi")


def _artifacts_exist(desc):
    data = desc.get("data") or {}
    for key in ("geojson_path", "image_path", "points_path"):
        p = data.get(key)
        if p and not os.path.exists(p):
            return False
    return True


def _err(msg):
    return {
        "status": "error",
        "message": msg,
        "kind": None,
        "bounds": None,
        "data": {},
        "warnings": [],
    }


def _union_bounds(boxes):
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def _vector_layer(target, port, layer):
    """MVT tile descriptor for one layer of `target` (layer=None for a
    single-layer file). Kicks off the daemon's warm-up for that layer."""
    import urllib.parse
    import urllib.request

    import geo_classify

    params = {"file": target}
    if layer:
        params["layer"] = layer
    qs = urllib.parse.urlencode(params)

    meta = geo_classify.vector_meta(target, layer)
    b = meta.get("bounds")
    # Latitude is the hard bound; longitude allows both the -180..180 and the
    # 0..360 (Pacific/marine) conventions before a file is called non-geographic.
    if b and (b[0] < -180 or b[2] > 360 or b[1] < -90 or b[3] > 90):
        return {
            **_err(
                "coordinates fall outside the valid lon/lat range — this file doesn't look "
                "georeferenced (e.g. image/pixel coordinates)"
            ),
            "status": "not_georeferenced",
            "layer": layer,
            "name": layer or os.path.basename(target),
        }
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/open?{qs}", timeout=5).read()
    except Exception:
        pass

    return {
        "status": "ok",
        "kind": "vector_tiles_mvt",
        "layer": layer,
        "name": layer or os.path.basename(target),
        "crs_original": meta.get("crs_original"),
        "bounds": meta.get("bounds"),
        "data": {
            "port": port,
            "file": target,
            "layer": layer,
            "tile_url": f"http://127.0.0.1:{port}/tile/{{z}}/{{x}}/{{y}}.mvt?{qs}",
            "status_url": f"http://127.0.0.1:{port}/status?{qs}",
            "meta_url": f"http://127.0.0.1:{port}/meta?{qs}",
        },
        "stats": {
            "geometry_types": [meta.get("geometry_type")],
            "columns": {c: "" for c in meta.get("columns", [])},
        },
        "style": _default_vector_style(meta.get("geometry_type")),
        "minzoom": 0,
        "maxzoom": 18,
        "geometry_type": meta.get("geometry_type"),
        "columns": meta.get("columns", []),
        "warnings": [],
        "detected_type": "vector",
    }


def _vector_tiles(target):
    """Ensure the MVT daemon is running and return a tile descriptor. A
    multi-layer source (GPKG/KML/GML) returns a `vector_group` listing one
    child descriptor per layer; a single-layer source returns the child."""
    import geo_classify
    import vector_tile_server as vts

    ensured = vts.main("ensure")
    port = ensured.get("port")
    if not port:
        return _err(ensured.get("error", "tile daemon failed to start"))

    layers = geo_classify.list_layers(target)
    if not layers:
        try:
            return _vector_layer(target, port, None)
        except Exception as e:  # noqa: BLE001
            return _err(f"couldn't read vector file: {type(e).__name__}: {e}")

    children = []
    for lname in layers:
        try:
            children.append(_vector_layer(target, port, lname))
        except Exception as e:  # noqa: BLE001
            children.append({**_err(f"{type(e).__name__}: {e}"), "layer": lname, "name": lname})
    ok = [c["bounds"] for c in children if c.get("status") == "ok" and c.get("bounds")]
    return {
        "status": "ok",
        "kind": "vector_group",
        "file": target,
        "bounds": _union_bounds(ok) if ok else None,
        "layers": children,
        "detected_type": "vector",
    }


def _raster_tiles(target, colormap, rescale):
    """Ensure the daemon is up and return an XYZ PNG tile descriptor for a
    georeferenced raster (reprojected + colormapped per tile by the daemon)."""
    import json
    import urllib.parse
    import urllib.request

    import vector_tile_server as vts

    ensured = vts.main("ensure")
    port = ensured.get("port")
    if not port:
        return _err(ensured.get("error", "tile daemon failed to start"))

    qs = urllib.parse.urlencode({"file": target})
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/rmeta?{qs}", timeout=120) as r:
            meta = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        return _err(f"raster meta failed: {type(e).__name__}: {e}")
    if not meta.get("supported"):
        return {
            **_err(meta.get("error") or "raster not supported"),
            "status": "not_georeferenced",
            "message": meta.get("error"),
        }

    st = meta.get("stretch") or [[None, None]]
    rmode = meta.get("render_mode", "single")
    tile_url = (
        f"http://127.0.0.1:{port}/rtile/{{z}}/{{x}}/{{y}}.png?{qs}"
        f"&colormap={colormap}&rescale={rescale}"
    )
    band_stats = [{"index": 1, "p2": st[0][0], "p98": st[0][1], "min": st[0][0], "max": st[0][1]}]
    return {
        "status": "ok",
        "kind": "raster_tiles",
        "crs_original": meta.get("crs"),
        "bounds": meta.get("bounds_4326"),
        "data": {
            "port": port,
            "file": target,
            "tile_url": tile_url,
            "rmeta_url": f"http://127.0.0.1:{port}/rmeta?{qs}",
        },
        "stats": {
            "bands": meta.get("bands"),
            "width": meta.get("width"),
            "height": meta.get("height"),
            "dtype": meta.get("dtype"),
            "nodata": meta.get("nodata"),
            "band_stats": band_stats,
            "render_mode": rmode,
        },
        "style": {
            "opacity": 0.9,
            "colormap": colormap,
            "rescale": (st[0] if rmode == "single" else None),
            "render_mode": rmode,
        },
        "minzoom": 0,
        "maxzoom": meta.get("maxzoom", 22),
        "warnings": [],
        "detected_type": "GeoTIFF/raster",
    }


def _default_vector_style(gtype):
    g = (gtype or "").lower()
    style = {
        "opacity": 1.0,
        "fill_color": [56, 135, 255, 90],
        "line_color": [90, 200, 255, 220],
        "line_width": 1.5,
        "point_radius": 5,
        "color_by": None,
        "colormap": "viridis",
    }
    if "point" in g and "polygon" not in g and "line" not in g:
        style["fill_color"] = [0, 200, 255, 220]
    return style


def _cached(target, mtime, opts):
    opts_json = json.dumps(opts, sort_keys=True)
    artifact_id = hashlib.sha256(f"{target}|{mtime}|{opts_json}".encode()).hexdigest()[:16]
    cache_dir = os.path.join(CACHE_ROOT, artifact_id)
    desc_path = os.path.join(cache_dir, "descriptor.json")

    if os.path.exists(desc_path):
        try:
            with open(desc_path, encoding="utf-8") as fh:
                cached = json.load(fh)
            if _artifacts_exist(cached):
                return cached
        except (OSError, json.JSONDecodeError):
            pass

    os.makedirs(cache_dir, exist_ok=True)
    import geo_classify

    desc = geo_classify.classify(target, cache_dir, artifact_id, opts)

    tmp = f"{desc_path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(desc, fh)
    os.replace(tmp, desc_path)
    return desc


def main(target: str = "", colormap: str = "viridis", rescale: str = ""):
    """Classify `target` (a geospatial file path or URL) and return a map-layer
    descriptor. colormap/rescale style a single-band raster (rescale = "lo,hi")."""
    target = (target or "").strip()
    if not target:
        return {
            "status": "error",
            "message": "No file selected.",
            "kind": None,
            "bounds": None,
            "data": {},
            "warnings": [],
        }

    is_url = target.startswith(_URLISH)
    if not is_url:
        target = os.path.abspath(os.path.expanduser(target))
        if not os.path.exists(target):
            return {
                "status": "error",
                "message": f"Not found: {target}",
                "kind": None,
                "bounds": None,
                "data": {},
                "warnings": [],
            }
        mtime = os.path.getmtime(target)
    else:
        mtime = 0.0

    import geo_classify

    route = geo_classify.route_target(target)
    if route == "vector":
        return _vector_tiles(target)
    if route == "raster":
        return _raster_tiles(target, colormap, rescale)

    opts = {"colormap": colormap}
    if rescale:
        try:
            lo, hi = (float(x) for x in rescale.split(","))
            opts["rescale"] = [lo, hi]
        except ValueError:
            pass
    return _cached(target, mtime, opts)


try:
    import fused as _fused

    _udf_main = _fused.udf(main)
except ImportError:
    pass
