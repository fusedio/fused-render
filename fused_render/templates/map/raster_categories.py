"""Categorical (unique-value / paletted) raster classification shared by
raster_engine.py's tiled path and geo_classify.py's one-shot path.
"""
from __future__ import annotations

from typing import Any

CATEGORY_CAP = 30

# Mirrors the CATEGORICAL swatch array in template.html so raster and vector
# unique-value legends use the same colors.
PALETTE: tuple[tuple[int, int, int], ...] = (
    (255, 99, 71), (70, 193, 216), (255, 193, 7), (126, 211, 33), (171, 71, 188),
    (255, 138, 101), (41, 182, 246), (141, 110, 99), (236, 64, 122), (102, 187, 106),
)


def _parse_override_color(value: Any) -> tuple[int, int, int, int] | None:
    """Accept a "#rrggbb" hex string or an [r,g,b,(a)] sequence from the UI."""
    if isinstance(value, str):
        text = value.lstrip("#")
        if len(text) != 6:
            return None
        try:
            r, g, b = (int(text[i : i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return None
        return (r, g, b, 255)
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        r, g, b = int(value[0]), int(value[1]), int(value[2])
        a = int(value[3]) if len(value) > 3 else 255
        return (r, g, b, a)
    return None


def classify_categories(
    values,
    dtype: str,
    embedded_colormap: dict[int, tuple[int, ...]] | None = None,
    overrides: dict[str, Any] | None = None,
    labels: dict[int, str] | None = None,
    cap: int = CATEGORY_CAP,
) -> list[dict[str, Any]] | None:
    """Return a per-value legend for a categorical raster, or ``None`` if
    *values* doesn't look like categorical data.

    ``values`` is a sample of the band's pixel values (e.g. from a coarse
    preview read) — it doesn't need to be exhaustive, just representative;
    it may be a masked array (masked/nodata pixels are dropped).

    ``embedded_colormap`` is GDAL's per-band color table
    (``{int: (r,g,b,a)}``, from rasterio's ``dataset.colormap(band)``) when
    the file ships its own palette (common for land-cover products); its
    presence alone makes the raster eligible, and its colors take
    precedence over the fallback ``PALETTE``. ``overrides`` is
    ``{str(value): "#hex" | [r,g,b,a]}`` from user edits in the style dock;
    it wins over both. ``labels`` is ``{value: class name}`` (e.g. from a
    PAM raster attribute table); unnamed values keep ``str(value)``.
    """
    import numpy as np

    is_int = np.issubdtype(np.dtype(dtype), np.integer)
    sample = np.ma.compressed(values) if np.ma.is_masked(values) else np.asarray(values).ravel()
    sample = sample[np.isfinite(sample)]
    if sample.size == 0:
        return None
    unique, sample_counts = np.unique(sample, return_counts=True)
    counts = {int(v): int(c) for v, c in zip(unique, sample_counts)}

    eligible = bool(embedded_colormap) or (is_int and len(unique) <= cap)
    if not eligible:
        return None

    if embedded_colormap:
        candidates = sorted(int(v) for v in unique if int(v) in embedded_colormap)
        if not candidates:
            # The table doesn't cover what's actually in the data (e.g. a
            # stale/partial one) — fall back to the raw sample, capped like
            # the heuristic path below.
            candidates = sorted(int(v) for v in unique)[:cap]
    else:
        candidates = sorted(int(v) for v in unique)[:cap]

    overrides = overrides or {}
    categories = []
    for i, value in enumerate(candidates):
        override = _parse_override_color(overrides.get(str(value)))
        if override is not None:
            color = override
        elif embedded_colormap and value in embedded_colormap:
            raw = tuple(int(c) for c in embedded_colormap[value])
            color = raw if len(raw) == 4 else raw[:3] + (255,)
        else:
            color = PALETTE[i % len(PALETTE)] + (255,)
        name = labels.get(value) if labels else None
        categories.append(
            {
                "value": value,
                "label": str(value) if name is None else str(name),
                "color": list(color),
                "count": counts.get(value, 0),
            }
        )
    return categories


# GDALRATFieldUsage codes from gdal.h.
_GFU_NAME = 2
_GFU_MINMAX = 5
_GFU_RED, _GFU_GREEN, _GFU_BLUE, _GFU_ALPHA = 6, 7, 8, 9

_RAT_NAME_ALIASES = {
    "value": ("value",),
    "red": ("red",),
    "green": ("green",),
    "blue": ("blue",),
    "alpha": ("alpha",),
    "name": ("class_name", "classname", "class name", "category", "label", "class", "name"),
}


def _rat_column(fields: list[tuple[str, int]], role: str, usage: int) -> int | None:
    for index, (_, field_usage) in enumerate(fields):
        if field_usage == usage:
            return index
    aliases = _RAT_NAME_ALIASES[role]
    for index, (name, _) in enumerate(fields):
        if name.lower() in aliases:
            return index
    return None


def _parse_pam_band(band) -> tuple[dict[int, tuple[int, int, int, int]] | None, dict[int, str] | None]:
    colors = None
    table = band.find(".//ColorTable")
    if table is not None:
        colors = {
            index: (
                int(entry.get("c1", 0)),
                int(entry.get("c2", 0)),
                int(entry.get("c3", 0)),
                int(entry.get("c4", 255)),
            )
            for index, entry in enumerate(table.findall("Entry"))
        } or None

    labels = None
    rat = band.find(".//GDALRasterAttributeTable")
    if rat is not None:
        fields = [
            (defn.findtext("Name", ""), int(defn.findtext("Usage", "0")))
            for defn in rat.findall("FieldDefn")
        ]
        value_col = _rat_column(fields, "value", _GFU_MINMAX)
        name_col = _rat_column(fields, "name", _GFU_NAME)
        rgb_cols = [
            _rat_column(fields, role, usage)
            for role, usage in (("red", _GFU_RED), ("green", _GFU_GREEN), ("blue", _GFU_BLUE))
        ]
        alpha_col = _rat_column(fields, "alpha", _GFU_ALPHA)
        rat_colors: dict[int, tuple[int, int, int, int]] = {}
        rat_labels: dict[int, str] = {}
        for row_index, row in enumerate(rat.findall("Row")):
            cells = [cell.text or "" for cell in row.findall("F")]
            value = (
                int(float(cells[value_col]))
                if value_col is not None and value_col < len(cells)
                else row_index
            )
            if name_col is not None and name_col < len(cells) and cells[name_col].strip():
                rat_labels[value] = cells[name_col].strip()
            if all(col is not None and col < len(cells) for col in rgb_cols):
                r, g, b = (int(float(cells[col])) for col in rgb_cols)
                a = (
                    int(float(cells[alpha_col]))
                    if alpha_col is not None and alpha_col < len(cells)
                    else 255
                )
                rat_colors[value] = (r, g, b, a)
        # An embedded ColorTable outranks RAT colors, matching GDAL itself.
        if colors is None and rat_colors:
            colors = rat_colors
        if rat_labels:
            labels = rat_labels
    return colors, labels


def read_pam_aux_xml(
    raster_path: str,
) -> tuple[dict[int, tuple[int, int, int, int]] | None, dict[int, str] | None]:
    """Class colors/labels from GDAL's PAM sidecar (``<raster_path>.aux.xml``).

    Covers what rasterio has no API for: raster attribute tables, plus the
    ColorTable case when GDAL's own sidecar discovery is disabled. The
    sidecar is optional metadata, so any read/parse problem yields
    ``(None, None)`` rather than failing the raster load.
    """
    import os
    import xml.etree.ElementTree as ET

    path = raster_path + ".aux.xml"
    try:
        if not os.path.isfile(path):
            return None, None
        root = ET.parse(path).getroot()
        bands = root.findall(".//PAMRasterBand")
        band = next((b for b in bands if b.get("band") == "1"), bands[0] if bands else None)
        if band is None:
            return None, None
        return _parse_pam_band(band)
    except Exception:
        return None, None


def resolve_render_mode(
    count: int,
    categories: list[dict[str, Any]] | None,
    embedded_colormap: dict[int, tuple[int, ...]] | None,
    requested_mode: str,
) -> str:
    """Decide "rgb" / "categorical" / "single" from band count, detected
    categories, whether an embedded GDAL colormap backs them, and any
    explicit ``opts["render_mode"]`` request (``""`` when nothing was
    requested).

    An embedded colormap is an authoritative signal ("this file IS paletted
    data") and auto-defaults to categorical; the bare unique-value heuristic
    (no colormap) is a weaker signal — plenty of ordinary continuous data is
    coarsely quantized (an 8-bit hillshade, a small DEM) — so it only
    switches to categorical on an explicit request, matching every other
    style choice (colormap, opacity, ...) in never silently changing how an
    already-open raster renders.
    """
    if count >= 3:
        return "rgb"
    if not categories:
        return "single"
    if requested_mode == "single":
        return "single"
    if requested_mode == "categorical":
        return "categorical"
    return "categorical" if embedded_colormap else "single"
