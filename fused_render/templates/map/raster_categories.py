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
    it wins over both.
    """
    import numpy as np

    is_int = np.issubdtype(np.dtype(dtype), np.integer)
    sample = np.ma.compressed(values) if np.ma.is_masked(values) else np.asarray(values).ravel()
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
        categories.append(
            {
                "value": value,
                "label": str(value),
                "color": list(color),
                "count": counts.get(value, 0),
            }
        )
    return categories


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
