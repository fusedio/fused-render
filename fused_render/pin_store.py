"""Pin persistence for the menu-bar pinned view (SPEC §25 PV-1/PV-7, D97).

One pinned filesystem path, stored as JSON at ``<app_support_dir>/pin.json``.
Pure python and cross-platform so it stays unit-testable; all AppKit code
lives in menubar_pin.py.
"""
import json
import logging
import os

logger = logging.getLogger("fused_render")

PIN_FILENAME = "pin.json"


def _pin_path(app_support_dir: str) -> str:
    return os.path.join(app_support_dir, PIN_FILENAME)


def load_pin(app_support_dir: str) -> str | None:
    """Return the pinned absolute path, or None when unset/unreadable.

    A corrupt or wrong-shaped pin.json is treated as "no pin" (and logged),
    never an error — losing the pin is a menu click to recover.
    """
    try:
        with open(_pin_path(app_support_dir)) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("unreadable pin.json (%s); treating as unpinned", exc)
        return None
    path = data.get("path") if isinstance(data, dict) else None
    if not isinstance(path, str) or not path:
        logger.warning("pin.json has no usable 'path'; treating as unpinned")
        return None
    return path


def save_pin(app_support_dir: str, path: str) -> None:
    os.makedirs(app_support_dir, exist_ok=True)
    with open(_pin_path(app_support_dir), "w") as f:
        json.dump({"path": path}, f)


def clear_pin(app_support_dir: str) -> None:
    try:
        os.remove(_pin_path(app_support_dir))
    except OSError:
        pass
