#!/usr/bin/env bash
# Build the FusedRender Linux desktop app as a single-file AppImage.
#
# Mirrors the STAGES of scripts/build_windows_installer.ps1 (wheel build →
# relocatable CPython → pip install the desktop extra → pre-install DuckDB
# extensions → copy uv → smoke tests → package), not its structure. The result
# is dist/FusedRender-<version>-x86_64.AppImage: download, `chmod +x`, run — no
# admin, no package manager.
#
# x86_64 only for now (aarch64 is a documented follow-up). Runs on Linux;
# `bash -n` / shellcheck verify it off-Linux, and CI (Task 8) runs it for real.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$REPO_ROOT/build/linux"
STAGE_DIR="$BUILD_DIR/AppDir"
PYTHON_ROOT="$STAGE_DIR/usr/python"
DIST_DIR="$REPO_ROOT/dist"
ARCH="x86_64"

# Pinned tool versions (verified by SHA256 like the ps1's pinned zig).
APPIMAGETOOL_VERSION="1.9.0"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/${APPIMAGETOOL_VERSION}/appimagetool-${ARCH}.AppImage"
# Static type-2 runtime so the produced AppImage runs WITHOUT libfuse2 on the
# host (gate (f): no FUSE2 assumption on Ubuntu 22.04 / Debian 12).
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-${ARCH}"

log() { printf '\n=== %s ===\n' "$*"; }

require() {
    command -v "$1" >/dev/null 2>&1 || { echo "required tool not found: $1" >&2; exit 1; }
}

require uv
require curl

# --- version (single-sourced in fused_render/__init__.py, per MEMORY) --------
VERSION="$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
    "$REPO_ROOT/fused_render/__init__.py")"
[ -n "$VERSION" ] || { echo "could not read __version__" >&2; exit 1; }
log "Building FusedRender (Linux AppImage) $VERSION"

mkdir -p "$BUILD_DIR" "$DIST_DIR"
rm -rf "$STAGE_DIR"
mkdir -p "$PYTHON_ROOT"

# --- wheel (hatch hook builds the React shell) -------------------------------
log "Building wheel"
export FUSED_RENDER_BRANCH=""
find "$DIST_DIR" -maxdepth 1 -name 'fused_render-*.whl' -delete 2>/dev/null || true
uv build --wheel --out-dir "$DIST_DIR" "$REPO_ROOT"
WHEEL="$(find "$DIST_DIR" -maxdepth 1 -name "fused_render-${VERSION}-*.whl" | head -n1)"
[ -n "$WHEEL" ] || { echo "wheel build did not produce fused-render $VERSION" >&2; exit 1; }

# --- relocatable CPython 3.12 (needs sci deps; py3.14 lacks duckdb/rasterio) --
log "Installing relocatable CPython 3.12"
PYTHON_CACHE="$BUILD_DIR/python-cache"
rm -rf "$PYTHON_CACHE"
uv python install 3.12 --install-dir "$PYTHON_CACHE" --no-bin --no-registry
RUNTIME_DIR="$(find "$PYTHON_CACHE" -maxdepth 1 -type d -name "cpython-3.12*-linux-*" | head -n1)"
[ -n "$RUNTIME_DIR" ] || { echo "uv did not install a CPython 3.12 Linux runtime" >&2; exit 1; }
rm -rf "$PYTHON_ROOT"
mv "$RUNTIME_DIR" "$PYTHON_ROOT"

BUNDLE_PYTHON="$PYTHON_ROOT/bin/python3"
[ -x "$BUNDLE_PYTHON" ] || { echo "staged Python runtime is incomplete" >&2; exit 1; }
rm -f "$PYTHON_ROOT/lib/python3.12/EXTERNALLY-MANAGED" 2>/dev/null || true

# --- desktop extra + DuckDB extensions ---------------------------------------
log "Installing wheel[bundled,fused,linux-desktop]"
uv pip install --python "$BUNDLE_PYTHON" "${WHEEL}[bundled,fused,linux-desktop]"

log "Pre-installing DuckDB extensions"
# Under bin/ (next to uv and rclone), because tools_dir — where the supervisor's
# child_environment points FUSED_RENDER_DUCKDB_EXTENSION_DIR — resolves to the
# interpreter's directory, which is $PYTHON_ROOT/bin on Linux (python3 lives in
# bin/), NOT $PYTHON_ROOT. Windows keeps python.exe directly in PythonRoot, so
# its ps1 stages them at PythonRoot; the destinations differ, the contract
# (tools_dir/duckdb_extensions) is identical.
DUCKDB_EXTENSIONS="$PYTHON_ROOT/bin/duckdb_extensions"
mkdir -p "$DUCKDB_EXTENSIONS"
"$BUNDLE_PYTHON" -I -c \
    "import duckdb, sys; con = duckdb.connect(config=dict(extension_directory=sys.argv[1])); [con.install_extension(name) for name in sys.argv[2:]]" \
    "$DUCKDB_EXTENSIONS" httpfs excel spatial

# --- copy uv into the payload (next to python, same idiom as the ps1) --------
UV_BIN="$(command -v uv)"
cp "$UV_BIN" "$PYTHON_ROOT/bin/uv"

# --- rclone bundled next to uv (same as the Windows installer / macOS DMG) ---
# The supervisor's child_environment points FUSED_RENDER_RCLONE_BIN here, so
# mounts work with zero user setup. Pinned release, published-SHA256 verified.
# fusermount3 stays HOST-provided: it is a setuid binary and cannot ship in an
# AppImage — a host without FUSE gets the existing "rclone/mount unavailable"
# error surface (mounts.py already branches to a plain FUSE mount off-darwin).
log "Bundling rclone"
require sha256sum
RCLONE_VERSION="1.74.4"
RCLONE_SHA256="fe435e0c36228e7c2f116a8701f01127bb1f694005fc11d1f27186c8bca4115d"
RCLONE_ZIP="$BUILD_DIR/rclone-v${RCLONE_VERSION}-linux-amd64.zip"
[ -f "$RCLONE_ZIP" ] || curl -fsSL \
    "https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-amd64.zip" \
    -o "$RCLONE_ZIP"
echo "${RCLONE_SHA256}  ${RCLONE_ZIP}" | sha256sum --check --status \
    || { echo "rclone zip SHA256 mismatch" >&2; exit 1; }
"$BUNDLE_PYTHON" -I -c \
    "import zipfile, sys, shutil, os; z = zipfile.ZipFile(sys.argv[1]); member = [n for n in z.namelist() if n.endswith('/rclone')][0]; dst = open(sys.argv[2], 'wb'); shutil.copyfileobj(z.open(member), dst); dst.close(); os.chmod(sys.argv[2], 0o755)" \
    "$RCLONE_ZIP" "$PYTHON_ROOT/bin/rclone"

# --- prune caches ------------------------------------------------------------
find "$PYTHON_ROOT" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

# --- smoke tests (drop the pywin32 imports; add supervisor + tray) -----------
log "Smoke tests"
# The Linux tray is StatusNotifierItem over D-Bus (dbus-fast), which imports
# without a display — so no headless-backend workaround is needed here.
"$BUNDLE_PYTHON" -I -c \
    "import duckdb, fused_render, fused_render.cli, fused_render.supervisor.core, fused_render.supervisor._linux.tree, fused_render.supervisor._linux.instance, fused_render.supervisor._linux.tray, dbus_fast; print('bundle imports ok')"
"$PYTHON_ROOT/bin/uv" --version
"$PYTHON_ROOT/bin/rclone" version
SMOKE_REQUEST="$(mktemp)"
SMOKE_PROBE="$(mktemp --suffix=.py)"
trap 'rm -f "$SMOKE_REQUEST" "$SMOKE_PROBE"' EXIT
printf 'import duckdb\ndef main():\n    return {"value": duckdb.sql("select 42").fetchone()[0]}\n' > "$SMOKE_PROBE"
printf '{"path": "%s", "params": {}}' "$SMOKE_PROBE" > "$SMOKE_REQUEST"
CHILD="$PYTHON_ROOT/lib/python3.12/site-packages/fused_render/_child.py"
OUTPUT="$("$BUNDLE_PYTHON" "$CHILD" < "$SMOKE_REQUEST")"
case "$OUTPUT" in
    *'"value"'*42*) : ;;
    *) echo "staged _child.py smoke test failed: $OUTPUT" >&2; exit 1 ;;
esac

# --- AppDir assembly ---------------------------------------------------------
log "Assembling AppDir"
install -m 0755 "$REPO_ROOT/scripts/linux/AppRun" "$STAGE_DIR/AppRun"

# Icon: derive a PNG from the shipping .ico (pillow is in [bundled]); place it
# both at the AppDir root (appimagetool requirement) and in the icon theme dir.
ICON_DIR="$STAGE_DIR/usr/share/icons/hicolor/256x256/apps"
mkdir -p "$ICON_DIR"
"$BUNDLE_PYTHON" -I -c \
    "from PIL import Image; import sys; Image.open(sys.argv[1]).convert('RGBA').resize((256, 256)).save(sys.argv[2])" \
    "$REPO_ROOT/fused_render/assets/fused-render.ico" "$ICON_DIR/fused-render.png"
cp "$ICON_DIR/fused-render.png" "$STAGE_DIR/fused-render.png"

# .desktop with the MimeType list generated from the shared association table.
MIMETYPE="$("$BUNDLE_PYTHON" -I "$REPO_ROOT/scripts/file_associations.py" mime-types)"
APPS_DIR="$STAGE_DIR/usr/share/applications"
mkdir -p "$APPS_DIR"
sed "s|@MIMETYPE@|${MIMETYPE}|" "$REPO_ROOT/scripts/linux/fused-render.desktop.in" \
    > "$APPS_DIR/fused-render.desktop"
cp "$APPS_DIR/fused-render.desktop" "$STAGE_DIR/fused-render.desktop"

# Custom freedesktop MIME package so "Open with" works for extensions with no
# standard shared-mime-info type — same shared association table.
MIME_DIR="$STAGE_DIR/usr/share/mime/packages"
mkdir -p "$MIME_DIR"
"$BUNDLE_PYTHON" -I "$REPO_ROOT/scripts/file_associations.py" mime-xml \
    "$MIME_DIR/fused-render.xml"

# --- package with appimagetool (pinned, static runtime → no libfuse2) --------
log "Packaging AppImage"
TOOLS_DIR="$BUILD_DIR/tools"
mkdir -p "$TOOLS_DIR"
APPIMAGETOOL="$TOOLS_DIR/appimagetool-${ARCH}.AppImage"
RUNTIME_FILE="$TOOLS_DIR/runtime-${ARCH}"
[ -f "$APPIMAGETOOL" ] || { curl -fsSL "$APPIMAGETOOL_URL" -o "$APPIMAGETOOL"; chmod +x "$APPIMAGETOOL"; }
[ -f "$RUNTIME_FILE" ] || curl -fsSL "$RUNTIME_URL" -o "$RUNTIME_FILE"

OUTPUT_APPIMAGE="$DIST_DIR/FusedRender-${VERSION}-${ARCH}.AppImage"
rm -f "$OUTPUT_APPIMAGE"
# --appimage-extract-and-run avoids needing libfuse2 to RUN appimagetool itself
# in CI; --runtime-file embeds the static type-2 runtime in the OUTPUT.
ARCH="$ARCH" "$APPIMAGETOOL" --appimage-extract-and-run \
    --runtime-file "$RUNTIME_FILE" "$STAGE_DIR" "$OUTPUT_APPIMAGE"
[ -f "$OUTPUT_APPIMAGE" ] || { echo "appimagetool did not produce $OUTPUT_APPIMAGE" >&2; exit 1; }
chmod +x "$OUTPUT_APPIMAGE"
log "AppImage: $OUTPUT_APPIMAGE"
