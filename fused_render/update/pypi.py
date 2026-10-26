"""CLI update notice: `fused-render serve` from a pip/uv/pipx install.

A packaged app updates itself from the signed manifest (mac.py / linux.py). A
CLI install has no bundle to swap, and it upgrades from PyPI anyway, so this
manager asks PyPI for the newest release instead — which also means it needs
no `cryptography` (a lean install lacks it). It is check-only: it never
installs anything. It reports the newer version through the same
/api/config `update` field (the Updates dock shows it with the terminal
command to run, `manual_command`) and prints one line to stderr per process.

Only `fused-render serve` starts it (`update.enable_cli_check()`), never
create_app() itself, so tests and embedded servers never touch the network.
"""
from __future__ import annotations

import json
import logging
import sys
import threading

from fused_render.update import _manager as _base
from fused_render.update import common

logger = logging.getLogger(__name__)

PYPI_URL = "https://pypi.org/pypi/fused-render/json"
MAX_BYTES = 4 * 1024 * 1024


def installer_kind(prefix: str | None = None) -> str:
    """"uv" | "pipx" | "pip", from the interpreter's prefix."""
    p = (prefix if prefix is not None else sys.prefix).replace("\\", "/").lower()
    if "/uv/tools/" in p:
        return "uv"
    if "/pipx/venvs/" in p:
        return "pipx"
    return "pip"


_COMMANDS = {
    "uv": "uv tool upgrade fused-render",
    "pipx": "pipx upgrade fused-render",
    "pip": "pip install -U fused-render",
}


def _parse(version: str) -> tuple[int, ...] | None:
    try:
        parts = tuple(int(x) for x in version.split("."))
    except (ValueError, AttributeError):
        return None
    return parts or None


class PyPIUpdateManager(_base.UpdateManager):
    def __init__(self) -> None:
        super().__init__(manifest_url=PYPI_URL, bundle=None, method=None, check_only=True)
        self._announced = False

    def method(self) -> str:
        return installer_kind()

    def _disk_version(self) -> str | None:
        return None

    def _manual_command(self) -> str | None:
        return _COMMANDS[installer_kind()]

    def _fetch_latest(self) -> dict:
        with common.urlopen(PYPI_URL, common.FETCH_TIMEOUT_S) as resp:
            raw = resp.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("PyPI response is too large")
        return {"version": str(json.loads(raw)["info"]["version"])}

    def _is_newer(self, version: str) -> bool:
        # A dev/local build ("0.6.98.dev1+g123") is not plain dotted ints:
        # quietly say "no update" rather than raise.
        latest, running = _parse(version), _parse(self._running_version())
        if latest is None or running is None:
            return False
        return latest > running

    def _on_update_found(self, manifest: dict) -> None:
        with self._lock:
            if self._announced:
                return
            self._announced = True
        print(
            f"fused-render {manifest['version']} is available (you have "
            f"{self._running_version()}) — run: {self._manual_command()}",
            file=sys.stderr,
        )


_manager: PyPIUpdateManager | None = None
_manager_lock = threading.Lock()


def manager() -> PyPIUpdateManager | None:
    return _manager


def start() -> PyPIUpdateManager | None:
    """Create the singleton and start its background checks; idempotent."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = PyPIUpdateManager()
            _manager.start_auto_checks()
        return _manager
