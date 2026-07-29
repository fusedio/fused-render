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
    """Build an actionable error for a missing optional feature runtime."""
    missing = list(dict.fromkeys(packages))
    if not missing:
        return None
    names = ", ".join(missing)
    command = "uv pip install " + " ".join(missing)
    return (
        f"Optional support for {feature} requires Python packages that are "
        f"not installed: "
        f"{names}. Install them in the environment that runs Fused Render, "
        f"then restart it:\n{command}"
    )


def require(feature: str, requirements: Mapping[str, str]) -> str | None:
    """Return an install hint when a feature's imports are unavailable."""
    return dependency_message(feature, missing_packages(requirements))
