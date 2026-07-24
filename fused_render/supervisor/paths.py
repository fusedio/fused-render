"""Desktop supervisor path layout.

Distinct from fused_render/paths.py (the generic dev/state helpers the server
itself uses): this module is the desktop supervisor's own view of where its
state/cache/runtime/temp/logs live, and how it builds the child server
process's environment block.

Durable state IS the flat ~/.fused-render dotdir on both Linux and Windows —
byte-for-byte the same dir the dev/CLI and the released macOS app use
(shell/storage.home_dir() with no FUSED_RENDER_HOME set), so mounts land at
~/.fused-render/mounts and all user config lives in one known place. Sharing
with the dev/CLI is the product intent, not an accident: the desktop app and
the CLI operate on the same mounts.json, prefs, and templates. logs/ and temp/
are subdirs of that root. The disposable cache stays OS-native
($XDG_CACHE_HOME on Linux, %LOCALAPPDATA% on Windows) to stay out of backup
scope; on Linux runtime stays on $XDG_RUNTIME_DIR (tmpfs, 0700, socket-safe).
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path


# Python interpreter-identity vars stripped from a child's inherited env: a
# bundled child interpreter must never latch onto its parent's home/path.
# Single-sourced here (this module owns the child env-block contract) and shared
# by both backends — the POSIX plain merge below, and _win32/job.py's
# case-folding variant, which imports this same tuple.
STRIPPED_ENV_VARS = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONINSPECT",
)


def environment_block(overrides: dict[str, str] | None) -> dict[str, str]:
    """Complete child environment: the current process env minus the Python
    interpreter-identity vars (STRIPPED_ENV_VARS), plus `overrides`. This is the
    plain, case-sensitive merge the POSIX backends use directly; the Windows
    backend needs a case-folding variant (env var names are case-insensitive
    there) and keeps its own in _win32/job.py, built on the same tuple."""
    env = {name: value for name, value in os.environ.items() if name not in STRIPPED_ENV_VARS}
    env.update(overrides or {})
    return env


def _xdg_home(env_var: str, default_rel: str) -> Path:
    """An XDG base dir: `$env_var` if it is set to an absolute path, else
    `~/default_rel`. The spec says a relative value must be ignored."""
    value = os.environ.get(env_var)
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / default_rel


def linux_runtime_dir() -> Path:
    """The 0700 runtime dir holding the single-instance lock + IPC socket.

    `$XDG_RUNTIME_DIR/fused-render` when the session sets it (the correct,
    per-user, tmpfs-backed home for sockets and locks); otherwise a private
    `runtime/` under the XDG cache dir. Single-sourced here because
    `_linux/instance.py` needs it during `acquire()` — before `DesktopPaths`
    exists — and `DesktopPaths.discover()` points its `runtime` field at the
    same place.
    """
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime and os.path.isabs(xdg_runtime):
        return Path(xdg_runtime) / "fused-render"
    return _xdg_home("XDG_CACHE_HOME", ".cache") / "fused-render" / "runtime"


@dataclass(frozen=True)
class DesktopPaths:
    root: Path
    state: Path
    cache: Path
    runtime: Path
    temp: Path
    logs: Path

    @classmethod
    def discover(cls) -> "DesktopPaths":
        if sys.platform.startswith("linux"):
            return cls.discover_linux()
        # Windows. Durable state IS the flat ~/.fused-render dotdir (one known
        # place, shared with the dev/CLI by design — same layout as Linux and
        # macOS); logs/temp/runtime are subdirs of it. Only the disposable
        # cache stays OS-native under %LOCALAPPDATA%. LOCALAPPDATA missing is
        # not fatal — it only steers cache, so fall back to a cache dir under
        # the dotdir root rather than raising.
        root = Path.home() / ".fused-render"
        local_app_data = os.environ.get("LOCALAPPDATA")
        cache = (
            Path(local_app_data) / "FusedRender" / "cache"
            if local_app_data
            else root / "cache"
        )
        return cls(
            root=root,
            state=root,
            cache=cache,
            runtime=root / "runtime",
            temp=root / "temp",
            logs=root / "logs",
        )

    @classmethod
    def discover_linux(cls) -> "DesktopPaths":
        """Flat dotdir layout: durable state IS ~/.fused-render — the exact dir
        the dev/CLI and the released macOS app use (shell/storage.home_dir()
        with no FUSED_RENDER_HOME), shared with them by design so mounts land
        at ~/.fused-render/mounts and all user config lives in one known
        place. logs/ and temp/ are subdirs of that root. The disposable cache
        stays OS-native under $XDG_CACHE_HOME (~/.cache) so its GBs of
        uv/rclone/duckdb caches stay out of backup scope; runtime stays under
        $XDG_RUNTIME_DIR (see linux_runtime_dir — tmpfs, 0700, socket-safe).
        XDG_DATA_HOME no longer steers the root. child_environment() is
        contract-identical to Windows regardless — same FUSED_RENDER_* keys.

        Exposed as its own classmethod (not folded into discover) so the pure
        path computation is unit-testable on any platform, not only Linux.
        """
        root = Path.home() / ".fused-render"
        cache_root = _xdg_home("XDG_CACHE_HOME", ".cache") / "fused-render"
        return cls(
            root=root,
            state=root,
            cache=cache_root,
            runtime=linux_runtime_dir(),
            temp=root / "temp",
            logs=root / "logs",
        )

    @classmethod
    def under(cls, root: Path) -> "DesktopPaths":
        """Everything under one root (state IS the root, matching discover);
        handy for tests and sandboxed layouts."""
        return cls(
            root=root,
            state=root,
            cache=root / "cache",
            runtime=root / "runtime",
            temp=root / "temp",
            logs=root / "logs",
        )

    def create(self) -> None:
        for path in (self.state, self.cache, self.runtime, self.temp, self.logs):
            path.mkdir(parents=True, exist_ok=True)

    def log(self, message: str) -> None:
        """Best-effort append to logs/supervisor.log. Never raises — shared by
        the fatal-error handler in __main__.py and by subsystems (tray) that
        must warn without ever treating the warning as fatal to the Job-owned
        Python server."""
        try:
            self.logs.mkdir(parents=True, exist_ok=True)
            with open(self.logs / "supervisor.log", "a", encoding="utf-8") as f:
                f.write(f"{int(time.time())}: {message}\n")
        except OSError:
            pass

    def self_environment(self) -> dict[str, str]:
        """Env applied to the supervisor process itself (before it imports
        anything else from fused_render), so incidental fused_render imports
        in-process never latch onto a dev checkout's paths."""
        return {
            "FUSED_RENDER_HOME": str(self.state),
            "FUSED_RENDER_CACHE_DIR": str(self.cache),
            "FUSED_RENDER_LOG_DIR": str(self.logs),
            # Explicit baseline opt-out (bugbot #5): desktop state is a flat
            # root, never nested under a branch subfolder — _branch.py treats
            # an explicitly-set empty FUSED_RENDER_BRANCH as "no isolation".
            "FUSED_RENDER_BRANCH": "",
            # Same rationale: RCLONE_PERSIST is a dev-shell iteration flag
            # (dev.sh sets it so rcd outlives watchfiles restarts). The
            # packaged app must never inherit it — a persisted, detached rcd
            # would outlive the app and dodge teardown.
            "FUSED_RENDER_RCLONE_PERSIST": "",
        }

    def child_environment(
        self, instance_id: str, token: str, tools_dir: Path
    ) -> dict[str, str]:
        openfused = self.state / "openfused"
        rclone = self.state / "rclone"
        env = {
            "FUSED_RENDER_HOME": str(self.state),
            "FUSED_RENDER_CACHE_DIR": str(self.cache),
            "FUSED_RENDER_RUNTIME_DIR": str(self.runtime),
            "FUSED_RENDER_TEMP_DIR": str(self.temp),
            "FUSED_RENDER_LOG_DIR": str(self.logs),
            "FUSED_RENDER_BRANCH": "",
            # Explicit production opt-out, mirroring self_environment: the
            # supervisor's children must not inherit dev-shell iteration flags
            # (dev.sh doesn't go through this method, so its own flag keeps
            # working).
            "FUSED_RENDER_RCLONE_PERSIST": "",
            "FUSED_RENDER_DESKTOP_INSTANCE_ID": instance_id,
            "FUSED_RENDER_DESKTOP_INSTANCE_TOKEN": token,
            "OPENFUSED_ENVS_FILE": str(openfused / "envs.json"),
            "OPENFUSED_FUSED_CLOUD_CREDENTIALS": str(openfused / "fused-cloud-credentials.json"),
            "OPENFUSED_SECRETS_FILE": str(openfused / "secrets.json"),
            "OPENFUSED_WORKSPACES_DIR": str(openfused / "workspaces"),
            "RCLONE_CONFIG": str(rclone / "rclone.conf"),
            "RCLONE_CACHE_DIR": str(self.cache / "rclone"),
            "UV_CACHE_DIR": str(self.cache / "uv"),
            "FUSED_RENDER_CLAUDE_DIR": str(self.state / "claude"),
            "CLAUDE_CONFIG_DIR": str(self.state / "claude"),
            "FUSED_RENDER_DUCKDB_EXTENSION_DIR": str(tools_dir / "duckdb_extensions"),
            "FUSED_RENDER_DUCKDB_TEMP_DIR": str(self.cache / "duckdb" / "temp"),
            # rclone is bundled in the payload next to the interpreter (and uv),
            # so mounts need zero user setup. mounts.rclone_bin() prefers this
            # over PATH guessing; a dev checkout without the file falls through.
            "FUSED_RENDER_RCLONE_BIN": str(
                tools_dir / ("rclone.exe" if sys.platform == "win32" else "rclone")
            ),
            # learn.zip ships in the payload's assets/ (the Windows installer
            # globs assets\*); mounts.learn_zip_path() reads this override, and
            # its isfile check no-ops when a build didn't bundle the zip.
            "FUSED_RENDER_LEARN_ZIP": str(tools_dir.parent / "assets" / "learn.zip"),
            "TEMP": str(self.temp),
            "TMP": str(self.temp),
            # POSIX tempfile consults TMPDIR first (TEMP/TMP are the Windows
            # conventions) — set all three so the child's temp files land in
            # the supervisor-owned temp dir on every platform.
            "TMPDIR": str(self.temp),
        }
        current_path = os.environ.get("PATH") or ""
        env["PATH"] = f"{tools_dir}{os.pathsep}{current_path}" if current_path else str(tools_dir)
        return env
