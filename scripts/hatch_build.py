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

try:
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface
except ModuleNotFoundError:  # hatchling only exists in a build env; allow the
    BuildHookInterface = object  # pure helpers below to be imported in tests


def _write_baked_ref(root: str, ref: str, build_data: dict) -> None:
    """Bake the ref into fused_render/_baked_branch.py, or remove it for a
    baseline build.

    Opt-in isolation: with a ref set, write it so the packaged artifact carries
    it without the env var, and register it as a build artifact (it's
    gitignored). With no ref (baseline), delete any stale baked file left by an
    earlier branch build — otherwise `_baked_ref()` would keep loading that old
    ref whenever FUSED_RENDER_BRANCH is unset, defeating the baseline.
    """
    baked_path = os.path.join(root, "fused_render", "_baked_branch.py")
    if not ref:
        if os.path.exists(baked_path):
            os.remove(baked_path)
        return
    with open(baked_path, "w") as f:
        f.write(f'_BAKED_REF = "{ref}"\n')
    build_data.setdefault("artifacts", []).append("fused_render/_baked_branch.py")


# The template starter kit ships the two canonical authoring skills so a
# scaffolded template carries its own guidance. The skills live once at
# skills/<name>/ (single source, D106); they are copied into the starter kit at
# build time (see _copy_starter_skills), gitignored there, and shipped via the
# `artifacts` glob in pyproject — the same not-committed-but-packaged pattern as
# the Vite shell (D54).
_STARTER_SKILLS = ("fused-render-authoring", "fused-render-custom-templates")


class ShellBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        if version == "editable":
            return

        self._bake_branch_ref(build_data)
        self._copy_starter_skills()

        frontend = os.path.join(self.root, "frontend")
        dist_index = os.path.join(self.root, "fused_render", "static", "shell-dist", "index.html")
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
        subprocess.run([npm, "install", "--no-audit", "--no-fund"], cwd=frontend, check=True)
        subprocess.run([npm, "run", "build"], cwd=frontend, check=True)

    def _copy_starter_skills(self) -> None:
        """Copy the canonical authoring skills into the starter kit's
        .claude/skills/ so every scaffolded template carries them. Source is the
        single repo-level skills/<name>/ (D106); the copies are gitignored and
        shipped via pyproject's `artifacts` glob. Refresh each time so a
        packaged build always reflects the current skill.
        """
        dest_root = os.path.join(self.root, "fused_render", "template_starter", ".claude", "skills")
        for name in _STARTER_SKILLS:
            src = os.path.join(self.root, "skills", name)
            dest = os.path.join(dest_root, name)
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            shutil.copytree(src, dest)

    def _bake_branch_ref(self, build_data: dict) -> None:
        """Resolve the ref from ``FUSED_RENDER_BRANCH`` and bake it into the
        packaged build (or clear it for a baseline build); see
        ``_write_baked_ref``.
        """
        sys.path.insert(0, self.root)
        try:
            from fused_render import _branch

            ref = _branch.branch_ref()
        finally:
            sys.path.remove(self.root)

        _write_baked_ref(self.root, ref, build_data)
