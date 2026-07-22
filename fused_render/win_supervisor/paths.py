"""Desktop supervisor path layout — port of windows/supervisor/src/paths.rs
(feat/windows-desktop-foundation, PR #162).

Distinct from fused_render/paths.py (the generic dev/state helpers the server
itself uses): this module is the Windows desktop supervisor's own view of
where its state/cache/runtime/temp/logs live, and how it builds the child
server process's environment block.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path


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
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise RuntimeError("LOCALAPPDATA is not set")
        return cls.under(Path(local_app_data) / "FusedRender" / "Desktop")

    @classmethod
    def under(cls, root: Path) -> "DesktopPaths":
        return cls(
            root=root,
            state=root / "state",
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
            # Explicit opt-out: desktop state is a flat root, never nested
            # under a branch subfolder — _branch.py treats an empty
            # FUSED_RENDER_BRANCH as "no isolation".
            "FUSED_RENDER_BRANCH": "",
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
            "TEMP": str(self.temp),
            "TMP": str(self.temp),
        }
        current_path = os.environ.get("PATH") or ""
        env["PATH"] = f"{tools_dir}{os.pathsep}{current_path}" if current_path else str(tools_dir)
        return env
