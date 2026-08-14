"""Detect optional Map Viewer dependencies without installing anything."""
from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterable, Mapping


def _is_available(module: str) -> bool:
    if sys.modules.get(module) is not None:
        return True
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def missing_packages(requirements: Mapping[str, str]) -> list[str]:
    """Return distribution names for unavailable import modules."""
    return list(
        dict.fromkeys(
            distribution
            for module, distribution in requirements.items()
            if not _is_available(module)
        )
    )


def dependency_message(feature: str, packages: Iterable[str]) -> str | None:
    """Build an actionable error for a missing optional feature runtime.

    The advice used to be "install them in the environment that runs Fused
    Render, then restart it: `uv pip install rasterio rio-tiler`". That was
    honest while these lived in `[bundled]` and the only way to be missing them
    was a source checkout, where a `uv pip install` is a thing the reader can do.

    D275 moved them into `map/pyproject.toml`, which makes this message reachable
    on a PACKAGED app for the first time — and a DMG user cannot pip install
    anything, so a message that only offers that is the exact defect D176 is
    about. The normal cause now is a project environment that has not finished
    building (or an engine that cannot build one at all — the built-in executor
    has no venv machinery, D174), so the message leads with that and keeps the
    manual command as the source-checkout fallback it always really was.
    """
    missing = list(dict.fromkeys(packages))
    if not missing:
        return None
    names = ", ".join(missing)
    command = "uv pip install " + " ".join(missing)
    return (
        f"Optional support for {feature} requires Python packages that are "
        f"not installed: "
        f"{names}. They are declared in this template's pyproject.toml, so the "
        f"app normally installs them into the Map Viewer's environment on first "
        f"render — wait for the one-time install to finish and reload. If no "
        f"install appears, the execution engine is set to Local (built-in), "
        f"which cannot build one: switch it to Fused in Preferences. In a source "
        f"checkout you can also install them yourself, then restart:\n{command}"
    )


def require(feature: str, requirements: Mapping[str, str]) -> str | None:
    """Return an install hint when a feature's imports are unavailable."""
    return dependency_message(feature, missing_packages(requirements))
