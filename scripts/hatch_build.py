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
    build_data.setdefault("artifacts", []).append(
        "fused_render/_baked_branch.py"
    )


# The canonical skills ship ONLY as a package-level copy at
# fused_render/skills/ — the wheel-install source for both the user-level skill
# sync (fused_render/user_skills.py, D185) and the plugin root assembled under
# home_dir() (fused_render/skill_plugin.py, D216). The skills live once at
# skills/<name>/ (single source, D106); the copy is gitignored and shipped via
# the `artifacts` glob in pyproject — the same not-committed-but-packaged
# pattern as the Vite shell (D54). Scaffolded folders (apps AND templates)
# carry no .claude/skills/ of their own any more (D185).
_ALL_SKILLS = (
    "fused-render-authoring",
    "fused-render-custom-templates",
    "fused-render-index",
    "fused-render-usage",
)


class ShellBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        if version == "editable":
            return

        self._bake_branch_ref(build_data)
        self._copy_starter_skills()

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

    def _copy_starter_skills(self) -> None:
        """Copy the canonical skills to fused_render/skills/ — the wheel-install
        source for the user-level skill sync (user_skills.py, D185) and for the
        plugin root assembled under home_dir() (skill_plugin.py, D216). Source is
        the single repo-level skills/<name>/; the copy is gitignored and shipped
        via pyproject's `artifacts` glob. Refresh each time so a packaged build
        always reflects the current skill. Starter kits (app and template) carry
        no .claude/ any more — scaffolded folders rely on the user-level sync —
        so any stale pre-D185 build copy is deleted rather than shipped (or
        copytree'd into new folders by a dev install).

        The plugin manifest travels alongside them, as a FLAT
        fused_render/skills/plugin.json rather than a packaged .claude-plugin/
        dir: nothing in the wheel may live under a dot-prefixed path, because
        whether a hidden path survives the backend's include globs is exactly
        the kind of thing that fails silently in a built wheel and nowhere else.
        skill_plugin.py mkdirs the dotted dir in its output instead.
        """
        for kit in ("app_starter", "template_starter"):
            shutil.rmtree(
                os.path.join(self.root, "fused_render", kit, ".claude"),
                ignore_errors=True,
            )
        dest_root = os.path.join(self.root, "fused_render", "skills")
        for name in _ALL_SKILLS:
            src = os.path.join(self.root, "skills", name)
            dest = os.path.join(dest_root, name)
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
        os.makedirs(dest_root, exist_ok=True)
        shutil.copyfile(
            os.path.join(self.root, ".claude-plugin", "plugin.json"),
            os.path.join(dest_root, "plugin.json"),
        )

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
