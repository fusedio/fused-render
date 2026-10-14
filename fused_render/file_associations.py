"""File-association data shared by the desktop packagers and the Linux runtime.

Derived from the template registry: which extensions fused-render registers as
an "Open with" handler, each extension's icon variant, and its canonical
shared-mime-info type where one exists. Consumed by `_mime_package.py` (Linux
runtime self-integration) and `scripts/file_associations.py` (AppImage build,
committed `file_associations.json`). Pure data — no platform code.
"""
import json
import os

_REGISTRY_JSON = os.path.join(os.path.dirname(__file__), "templates", "registry.json")
_NOT_EXTENSIONS = {".zgroup", ".zattrs", ".zmetadata"}  # zarr member files, not extensions

# Extension token (after the last dot) -> category icon variant, mirroring
# EXT_VARIANT in frontend/src/platform/ui/FileIcons.tsx so the association
# table names the same glyph fused-render's own listing shows for a file. Keep
# the two in sync. Anything unmapped falls back to the plain "file" icon.
_ICON_VARIANT_FOR_TOKEN = {
    # code / config / shell / style / markup
    **dict.fromkeys(
        "py js ts tsx jsx cjs mjs cts mts sh zsh fish ps1 csh zsh-theme vim "
        "yaml yml toml ini cfg conf tf hcl css plist twb tds".split(),
        "code",
    ),
    # tabular / gridded data
    **dict.fromkeys(
        "parquet csv tsv xlsx xlsm nc nc4 cdf h5 hdf hdf5 he5 zgroup zattrs "
        "zmetadata hyper".split(),
        "data",
    ),
    # structured text
    **dict.fromkeys("json jsonl ndjson".split(), "json"),
    # web
    **dict.fromkeys("html htm".split(), "html"),
    # images
    **dict.fromkeys(
        "png jpg jpeg gif webp svg bmp avif heic heif dng".split(), "image"
    ),
    # documents / prose
    **dict.fromkeys(
        "pdf md markdown txt log docx odt pptx tex ltx latex".split(), "doc"
    ),
    # audio / video
    **dict.fromkeys("mp4 mov m4v webm mp3 wav m4a ogg flac".split(), "media"),
    # geospatial / vector
    **dict.fromkeys(
        "geojson shp kml kmz gpx gpkg fgb pmtiles tif tiff las laz".split(), "geo"
    ),
    # archives
    **dict.fromkeys(
        "zip jar whl egg tar tgz tbz2 txz gz bz2 xz zst twbx tdsx bundle".split(),
        "archive",
    ),
    # databases
    **dict.fromkeys("sqlite sqlite3 db duckdb ddb".split(), "db"),
    # 3D models / point clouds
    **dict.fromkeys(
        "usd usda usdc usdz ply splat ksplat obj stl glb gltf".split(), "model"
    ),
}

# Canonical freedesktop shared-mime-info type per extension token, where one
# reliably exists in the shared MIME database. Consumed by the AppImage build
# (via scripts/file_associations.py) and the Linux runtime self-integration (supervisor/_linux/integration.py), which prefer the
# standard type over a custom `application/x-fused-render-<token>` glob — the
# Linux mirror of macOS "Alternate rank" (setup_py2app.py): never mint a new
# global type identity for an extension the shared database already names, and
# never steal the user's default handler for it.
#
# Deliberately conservative. An extension whose canonical type we are not sure
# is in the shared-mime-info database is LEFT OUT and falls back to the custom
# glob type, which always works. A wrong standard type is worse than the custom
# fallback: the desktop resolves a file to whatever type the database's globs
# assign, so naming a type the database won't assign drops the association
# entirely. That is why formats like parquet/pmtiles/fgb/laz, the whole 3D
# model family, and `.ts` (shared-mime-info calls it an MPEG transport stream,
# not TypeScript) are omitted here on purpose.
_STANDARD_MIME_FOR_TOKEN = {
    # code / config / shell / markup
    "py": "text/x-python",
    "js": "text/javascript",
    "mjs": "text/javascript",
    "css": "text/css",
    "html": "text/html",
    "htm": "text/html",
    "sh": "application/x-shellscript",
    "csh": "application/x-csh",
    "plist": "application/x-plist",
    "yaml": "application/yaml",
    "yml": "application/yaml",
    "toml": "application/toml",
    "tex": "text/x-tex",
    # structured / tabular data
    "json": "application/json",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "geojson": "application/geo+json",
    "nc": "application/x-netcdf",
    "cdf": "application/x-netcdf",
    # documents / prose
    "md": "text/markdown",
    "markdown": "text/markdown",
    "txt": "text/plain",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "odt": "application/vnd.oasis.opendocument.text",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    # images
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "svg": "image/svg+xml",
    "webp": "image/webp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "avif": "image/avif",
    "heic": "image/heic",
    "heif": "image/heif",
    "dng": "image/x-adobe-dng",
    # audio
    "mp3": "audio/mpeg",
    "wav": "audio/x-wav",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    # video
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    # archives
    "zip": "application/zip",
    "tar": "application/x-tar",
    "gz": "application/gzip",
    "bz2": "application/x-bzip2",
    "xz": "application/x-xz",
    "zst": "application/zstd",
    "tgz": "application/x-compressed-tar",
    "tbz2": "application/x-bzip-compressed-tar",
    "txz": "application/x-xz-compressed-tar",
    "jar": "application/x-java-archive",
    # geospatial
    "gpx": "application/gpx+xml",
    "kml": "application/vnd.google-earth.kml+xml",
    "kmz": "application/vnd.google-earth.kmz",
    # databases
    "sqlite": "application/vnd.sqlite3",
    "sqlite3": "application/vnd.sqlite3",
    "db": "application/vnd.sqlite3",
}


def standard_mime_for_token(token: str) -> str | None:
    """The canonical shared-mime-info type for an extension token (the part
    after the final dot, e.g. "py"), or None when no reliable standard type
    exists — in which case the custom `application/x-fused-render-<token>` glob
    type is used instead. Single source of truth for the standard-MIME tiering."""
    return _STANDARD_MIME_FOR_TOKEN.get(token.lower())


def extensions() -> list[str]:
    """Distinct extensions to register, each reduced to its final suffix —
    desktop associations key on the last one, so ".csv.zst" becomes ".zst" and
    the glob ".*.json" becomes ".json". Directory sentinels and zarr members
    drop out."""
    with open(_REGISTRY_JSON, encoding="utf-8") as f:
        registry = json.load(f)
    exts = set()
    for key in registry:
        if not key.startswith(".") or "/" in key or key in _NOT_EXTENSIONS:
            continue
        exts.add("." + key.rsplit(".", 1)[-1])
    return sorted(exts)
