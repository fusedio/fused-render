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
    path = _load_raw(app_support_dir).get("path")
    if path is None:
        return None
    if not isinstance(path, str) or not path:
        logger.warning("pin.json has no usable 'path'; treating as unpinned")
        return None
    return path


def _load_raw(app_support_dir: str) -> dict:
    try:
        with open(_pin_path(app_support_dir)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_raw(app_support_dir: str, data: dict) -> None:
    os.makedirs(app_support_dir, exist_ok=True)
    with open(_pin_path(app_support_dir), "w") as f:
        json.dump(data, f)


def save_pin(app_support_dir: str, path: str) -> None:
    # Merge, don't clobber: the popover size is a window preference, not a
    # property of the pinned file — it survives re-pins.
    data = _load_raw(app_support_dir)
    data["path"] = path
    _write_raw(app_support_dir, data)


def clear_pin(app_support_dir: str) -> None:
    # Only the pin goes; the remembered popover size survives an unpin/repin.
    data = _load_raw(app_support_dir)
    data.pop("path", None)
    if data:
        _write_raw(app_support_dir, data)
        return
    try:
        os.remove(_pin_path(app_support_dir))
    except OSError:
        pass


def load_size(app_support_dir: str) -> tuple[int, int] | None:
    """Return the saved popover (width, height), or None when unset/bad."""
    size = _load_raw(app_support_dir).get("size")
    if (
        isinstance(size, list)
        and len(size) == 2
        and all(isinstance(v, (int, float)) and v > 0 for v in size)
    ):
        return int(size[0]), int(size[1])
    return None


def save_size(app_support_dir: str, width: int, height: int) -> None:
    data = _load_raw(app_support_dir)
    data["size"] = [int(width), int(height)]
    _write_raw(app_support_dir, data)
