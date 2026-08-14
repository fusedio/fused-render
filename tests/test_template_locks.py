"""Every core template that declares an environment ships a matching `uv.lock`.

A mechanism, not a comment (D177). Two things go wrong without one, and neither
announces itself:

  * **A missing lock** means a shipped build resolves against PyPI the first time
    anyone renders that template. That is a network round trip and an unpinned
    resolution on a user's machine, for an environment we could have decided once
    at build time — and a resolution that can differ between two users of the
    same release.
  * **A stale lock** — one whose declared dependencies no longer match the
    manifest — silently costs the shipped build the thing the lock is for. The
    worker syncs BARE (not `--frozen`), so uv reconciles the difference on the
    user's machine: it resolves the missing part against PyPI at render time and
    rewrites the lock. Nothing fails, which is exactly why this has to be caught
    here — the release ships a lock that does not describe what users get, and
    two users of the same build can resolve different versions.

Both are checked against the FOLDER, which is where the environment is declared
(SPEC PY-16).
"""
import os

import pytest


def _import_toml():
    """tomllib (3.11+) or the tomli dependency that covers 3.10.

    Same shape as `tests/test_bundle_contents.py`'s. A bare `import tomllib` here
    raised ModuleNotFoundError at COLLECTION on 3.10, which errors the whole
    module out rather than skipping it. `tomli` is a declared dependency of this
    project (`fused_render/projectenv.py` reads manifests through the identical
    fallback), so on 3.10 these tests RUN — the skip is only for an install that
    genuinely has neither.
    """
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            pytest.skip("needs tomllib (3.11+) or the tomli package",
                        allow_module_level=True)
    return tomllib


tomllib = _import_toml()

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_REPO, "fused_render", "templates")


def _norm(requirement: str) -> str:
    """`"py360convert>=1.0.4"` -> `"py360convert"`. Same rule as the other suites."""
    import re

    return re.split(r"[<>=!~;\[ ]", requirement.strip())[0].lower().replace("_", "-")


def _declaring_folders() -> list[str]:
    return sorted(
        name for name in os.listdir(_TEMPLATES)
        if os.path.isfile(os.path.join(_TEMPLATES, name, "pyproject.toml"))
    )


def _load(path):
    with open(path, "rb") as f:
        return tomllib.load(f)


def test_at_least_one_template_declares_an_environment():
    """A guard on the guard: if the discovery breaks, everything below vacuously
    passes and the locks stop being checked at all."""
    assert _declaring_folders(), (
        "no template folder has a pyproject.toml — either the templates changed "
        "or this suite is looking in the wrong place"
    )


@pytest.mark.parametrize("folder", _declaring_folders())
def test_a_declaring_template_ships_a_lock(folder):
    lock = os.path.join(_TEMPLATES, folder, "uv.lock")
    assert os.path.isfile(lock), (
        f"fused_render/templates/{folder}/pyproject.toml declares an environment "
        "but ships no uv.lock, so a shipped build would resolve it against PyPI on "
        f"first render. Run `uv lock` in fused_render/templates/{folder} and "
        "commit the result."
    )


@pytest.mark.parametrize("folder", _declaring_folders())
def test_the_lock_matches_the_manifest(folder):
    """The lock's own record of the root's requirements must equal the manifest's.

    uv writes the resolved requirements of the workspace root back into the lock,
    so the two can be compared without re-resolving — which is the point: this
    has to be a cheap, offline, deterministic check, not a network call.
    """
    root = os.path.join(_TEMPLATES, folder)
    declared = {_norm(d) for d in _load(os.path.join(root, "pyproject.toml"))
                .get("project", {}).get("dependencies", [])}

    lock = _load(os.path.join(root, "uv.lock"))
    packages = lock.get("package", [])
    roots = [p for p in packages if p.get("source", {}).get("virtual") == "."]
    assert len(roots) == 1, (
        f"{folder}/uv.lock has {len(roots)} root entries; expected exactly the "
        "virtual project itself"
    )
    locked = {_norm(d["name"]) for d in roots[0].get("dependencies", [])}

    assert locked == declared, (
        f"fused_render/templates/{folder}/uv.lock is out of step with its "
        f"pyproject.toml (lock: {sorted(locked)}, manifest: {sorted(declared)}). "
        "The worker syncs bare, so uv would silently resolve the difference "
        "against PyPI on a user's machine and rewrite the lock — the shipped "
        "lock would not describe what users actually get. Run `uv lock` in "
        f"fused_render/templates/{folder} and commit the result."
    )


@pytest.mark.parametrize("folder", _declaring_folders())
def test_no_template_ships_an_in_folder_venv(folder):
    """The venv lives in the home dir, never here (MD-7).

    A `.venv` inside a core template would be destroyed on every release by
    `core_templates.py`'s rmtree+os.replace swap, costing a full re-download of
    numpy/pyproj/imagecodecs/pypandoc-binary each upgrade — and rmtree over a
    30k-file venv is slow, and fails on Windows while the geotiff and zarr
    daemons hold files open.
    """
    assert not os.path.exists(os.path.join(_TEMPLATES, folder, ".venv")), (
        f"fused_render/templates/{folder}/.venv exists; project venvs belong "
        "under ~/.fused-render/venvs (see fused_render/projectenv.py)"
    )
