"""Hatchling build hook: build the React shell into static/shell-dist.

The Vite output is NOT committed (D54): dev machines are expected to have
node and run `cd frontend && npm run build` themselves, while shipped
artifacts (wheel, and the DMG whose build venv pip-installs this repo) get
the shell built here, at package-build time. `artifacts` in pyproject.toml
lets hatchling ship the gitignored output.

Editable installs (`pip install -e .`) skip the build — the dev owns the
build/watch loop, and serve-from-source means the freshest local build wins.
"""
import os
import shutil
import subprocess
import sys

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class ShellBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        if version == "editable":
            return

        self._bake_branch_ref(build_data)

        frontend = os.path.join(self.root, "frontend")
        dist_index = os.path.join(
            self.root, "fused_render", "static", "shell-dist", "index.html"
        )
        if not os.path.isdir(frontend):
            # Building from a tree without frontend/ sources (shouldn't happen
            # — sdists include it); accept a pre-built shell, else fail loud.
            if os.path.exists(dist_index):
                return
            raise RuntimeError(
                "cannot build fused-render: frontend/ sources missing and "
                "fused_render/static/shell-dist/ not pre-built"
            )

        npm = shutil.which("npm")
        if npm is None:
            raise RuntimeError(
                "npm not found: building fused-render packages the React shell "
                "(frontend/ -> fused_render/static/shell-dist/), which needs "
                "Node 22. Install node or pre-build the shell."
            )
        subprocess.run(
            [npm, "install", "--no-audit", "--no-fund"], cwd=frontend, check=True
        )
        subprocess.run([npm, "run", "build"], cwd=frontend, check=True)

    def _bake_branch_ref(self, build_data: dict) -> None:
        """Write fused_render/_baked_branch.py with the resolved branch ref
        so packaged (non-editable) builds carry a stable ref without git.
        """
        sys.path.insert(0, self.root)
        try:
            from fused_render import _branch

            ref = _branch.branch_ref()
        finally:
            sys.path.remove(self.root)

        baked_path = os.path.join(self.root, "fused_render", "_baked_branch.py")
        with open(baked_path, "w") as f:
            f.write(f'_BAKED_REF = "{ref}"\n')

        build_data.setdefault("artifacts", []).append(
            "fused_render/_baked_branch.py"
        )
