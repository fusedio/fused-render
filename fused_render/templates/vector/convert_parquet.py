"""Convert a vector file (.shp/.kml/.kmz/...) to GeoParquet, next to the
source file.

Runs in the app's bundled runner (geopandas 1.1 + pyarrow write proper
GeoParquet 1.1 with CRS + bbox metadata). Reads the FULL file — no feature
cap — so the output is a faithful conversion even when the preview was
truncated. Never overwrites silently: an existing target gets a numbered
suffix unless `overwrite=1`.
"""


def main(file: str = "", overwrite: str = "0"):
    import os
    import time

    if not file:
        return {"error": "no file selected"}
    file = os.path.abspath(os.path.expanduser(file))
    if not os.path.isfile(file):
        return {"error": f"not a file: {file}"}

    import geopandas as gpd

    dst = file.rsplit(".", 1)[0] + ".parquet"
    if os.path.exists(dst) and str(overwrite) != "1":
        i = 1
        while os.path.exists(f"{file.rsplit('.', 1)[0]}_{i}.parquet"):
            i += 1
        dst = f"{file.rsplit('.', 1)[0]}_{i}.parquet"

    t0 = time.time()
    try:
        import pyogrio
        import pandas as pd
        layers = [str(l[0]) for l in pyogrio.list_layers(file)]
        if len(layers) <= 1:
            gdf = gpd.read_file(file)
        else:
            parts = []
            for name in layers:
                try:
                    part = gpd.read_file(file, layer=name)
                except Exception:  # noqa: BLE001
                    continue
                part = part[part.geometry.notna()]
                if len(part):
                    part.insert(0, "_layer", name)
                    parts.append(part)
            if not parts:
                return {"error": "no readable features in any layer"}
            gdf = gpd.GeoDataFrame(
                pd.concat(parts, ignore_index=True), crs=parts[0].crs)
    except Exception as e:  # noqa: BLE001
        return {"error": f"could not read vector file: {type(e).__name__}: {e}"}
    try:
        try:
            gdf.to_parquet(dst, compression="zstd",
                           geometry_encoding="WKB", write_covering_bbox=True)
        except TypeError:
            # older geopandas without bbox/encoding kwargs
            gdf.to_parquet(dst, compression="zstd")
    except Exception as e:  # noqa: BLE001
        return {"error": f"conversion failed: {type(e).__name__}: {e}"}

    return {
        "ok": True,
        "path": dst,
        "rows": int(len(gdf)),
        "size": os.path.getsize(dst),
        "src_size": os.path.getsize(file),
        "seconds": round(time.time() - t0, 2),
        "crs": (gdf.crs.to_epsg() if gdf.crs else None),
    }


try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
