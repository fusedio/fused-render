"""A .py's project folder, and that folder's central venv (fused_render/projectenv.py).

The environment a script runs in is decided by the folder it belongs to, not by
anything in the file. That makes the boundary rule the whole contract: get it
wrong and two scripts in one app silently run in two environments, or a stray
manifest three levels down hijacks the app's.

Isolation: every test points FUSED_RENDER_HOME and FUSED_RENDER_DIR at tmp_path,
so the real ~/.fused-render and ~/Documents/Fused are never touched — the same
discipline as tests/test_core_templates.py.
"""
import hashlib
import os

import pytest

from fused_render import projectenv


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway shell home + workspace, and a place to build project trees."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "workspace"))
    monkeypatch.delenv("FUSED_RENDER_CORE_TEMPLATES", raising=False)
    work = tmp_path / "work"
    work.mkdir()
    return work


def _write_project(d, deps=("cowsay",), *, table=True):
    d.mkdir(parents=True, exist_ok=True)
    body = "" if not table else (
        "[project]\nname = 'x'\nversion = '0.1.0'\n"
        "dependencies = [%s]\n" % ", ".join(repr(x) for x in deps)
    )
    (d / "pyproject.toml").write_text(body or "[tool.black]\nline-length = 88\n", encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# Boundary rule 1: an app folder is a project root
# ---------------------------------------------------------------------------


def test_app_dir_is_the_project_root(home, tmp_path):
    app = tmp_path / "workspace" / "tag" / "my-app"
    _write_project(app)
    (app / "page.py").write_text("x = 1\n", encoding="utf-8")

    assert projectenv.project_root_for(str(app / "page.py")) == str(app)


def test_app_dir_wins_however_deep_the_script_sits(home, tmp_path):
    app = tmp_path / "workspace" / "tag" / "my-app"
    _write_project(app)
    nested = app / "readers" / "deep"
    nested.mkdir(parents=True)
    (nested / "tiff.py").write_text("x = 1\n", encoding="utf-8")

    assert projectenv.project_root_for(str(nested / "tiff.py")) == str(app)


def test_nested_pyproject_inside_an_app_is_ignored(home, tmp_path):
    """A stray manifest below the root must not shadow the app's own."""
    app = tmp_path / "workspace" / "tag" / "my-app"
    _write_project(app, ["cowsay"])
    sub = app / "readers"
    _write_project(sub, ["altair"])
    (sub / "tiff.py").write_text("x = 1\n", encoding="utf-8")

    assert projectenv.project_root_for(str(sub / "tiff.py")) == str(app)
    assert projectenv.dependencies_of(str(app)) == ["cowsay"]


def test_app_dir_without_a_manifest_has_no_environment(home, tmp_path):
    app = tmp_path / "workspace" / "tag" / "plain-app"
    app.mkdir(parents=True)
    (app / "page.py").write_text("x = 1\n", encoding="utf-8")

    assert projectenv.project_root_for(str(app / "page.py")) == str(app)
    assert projectenv.has_project_env(str(app)) is False
    assert projectenv.project_env_for(str(app / "page.py")) is None


# ---------------------------------------------------------------------------
# Boundary rule 2/3: the two template dirs
# ---------------------------------------------------------------------------


def test_user_template_dir_is_a_project_root(home, tmp_path):
    tpl = tmp_path / "home" / "templates" / "mine"
    _write_project(tpl)
    (tpl / "helper.py").write_text("x = 1\n", encoding="utf-8")

    assert projectenv.project_root_for(str(tpl / "helper.py")) == str(tpl)


def test_core_template_dir_is_a_project_root(home, tmp_path):
    tpl = tmp_path / "home" / ".core-templates" / "geotiff"
    _write_project(tpl)
    deep = tpl / "lib"
    deep.mkdir()
    (deep / "reader.py").write_text("x = 1\n", encoding="utf-8")

    assert projectenv.project_root_for(str(deep / "reader.py")) == str(tpl)


def test_packaged_template_dir_is_a_project_root(home):
    """The in-bundle templates/ tree is a root too — tests and the dev override
    read templates from there rather than from the staged copy."""
    from fused_render import core_templates

    pkg = core_templates.PACKAGE_TEMPLATES_DIR
    py = os.path.join(pkg, "geotiff", "tiff_reader.py")
    if not os.path.exists(py):
        pytest.skip("packaged geotiff template not present")
    assert projectenv.project_root_for(py) == os.path.join(pkg, "geotiff")


def test_template_registry_json_is_not_a_root(home, tmp_path):
    """A file sitting directly in a template root has no template folder."""
    root = tmp_path / "home" / "templates"
    root.mkdir(parents=True)
    (root / "registry.json").write_text("{}", encoding="utf-8")

    assert projectenv.project_root_for(str(root / "registry.json")) is None


# ---------------------------------------------------------------------------
# Boundary rule 4: the TOPMOST ancestor declaring a pyproject.toml
# ---------------------------------------------------------------------------


def test_topmost_ancestor_wins_not_nearest(home):
    outer = _write_project(home / "outer", ["cowsay"])
    inner = _write_project(outer / "inner", ["altair"])
    (inner / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert projectenv.project_root_for(str(inner / "a.py")) == str(outer)


def test_no_manifest_anywhere_is_no_project(home):
    d = home / "loose"
    d.mkdir()
    (d / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert projectenv.project_root_for(str(d / "a.py")) is None
    assert projectenv.project_env_for(str(d / "a.py")) is None


def test_walk_stops_below_the_home_dirs_parent(home, tmp_path):
    """A manifest at the ceiling must not swallow everything beneath it.

    The ceiling is the shell home's parent — in production the user's home dir,
    where a stray pyproject.toml would otherwise turn the entire home into one
    project.
    """
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\nversion='1'\n", encoding="utf-8")
    d = home / "loose"
    d.mkdir()
    (d / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert projectenv.project_root_for(str(d / "a.py")) is None


def _use_branch(monkeypatch, ref):
    """Activate a branch ref. `_branch` caches the ref on first read per process,
    so the env var alone does nothing once anything has resolved it."""
    from fused_render import _branch

    monkeypatch.setenv("FUSED_RENDER_BRANCH", ref)
    monkeypatch.setattr(_branch, "_CACHED_REF", None)
    yield_back = _branch.branch_ref()
    assert yield_back, "the branch ref did not take effect"


def test_the_ceiling_does_not_move_when_a_branch_ref_is_set(tmp_path, monkeypatch):
    """The ceiling is the shell home's parent, and a branch must not raise it.

    `home_dir()` nests to `<base>/branches/<ref>` under FUSED_RENDER_BRANCH, so
    deriving the ceiling from it made the ceiling `<base>/branches` — a directory
    that is not an ancestor of anything the user works on. The walk for a file
    under `~` then never terminated at the ceiling at all, and a stray
    `~/pyproject.toml` swallowed the entire home directory into one project,
    which is the exact failure the ceiling exists to prevent.
    """
    base = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(base))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "workspace"))
    _use_branch(monkeypatch, "some-feature")

    # Sanity: the branch really does nest the home dir two levels down.
    from fused_render.shell.storage import home_dir

    assert home_dir() != str(base), "this test is vacuous without branch nesting"

    # The ceiling stands where the UN-nested base's parent is: tmp_path.
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='x'\nversion='1'\ndependencies=['cowsay']\n", encoding="utf-8"
    )
    d = tmp_path / "work" / "loose"
    d.mkdir(parents=True)
    (d / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert projectenv.project_root_for(str(d / "a.py")) is None, (
        "a manifest at the ceiling swallowed everything beneath it"
    )


def test_a_project_below_the_ceiling_still_resolves_under_a_branch(tmp_path, monkeypatch):
    """The guard must not over-reach either: real projects still resolve."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "workspace"))
    _use_branch(monkeypatch, "some-feature")

    proj = tmp_path / "work" / "app"
    proj.mkdir(parents=True)
    (proj / "pyproject.toml").write_text(
        "[project]\nname='x'\nversion='1'\ndependencies=['cowsay']\n", encoding="utf-8"
    )
    (proj / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert projectenv.project_root_for(str(proj / "a.py")) == str(proj)


def test_a_directory_argument_resolves_to_itself(home):
    proj = _write_project(home / "proj")
    assert projectenv.project_root_for(str(proj)) == str(proj)


# ---------------------------------------------------------------------------
# "Has an environment": a [project] table, not merely a pyproject.toml
# ---------------------------------------------------------------------------


def test_manifest_without_a_project_table_is_not_an_environment(home):
    proj = _write_project(home / "proj", table=False)
    (proj / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert projectenv.has_project_env(str(proj)) is False
    assert projectenv.project_env_for(str(proj / "a.py")) is None


def test_manifest_with_a_project_table_is_an_environment(home):
    proj = _write_project(home / "proj", ["cowsay", "altair"])
    (proj / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert projectenv.has_project_env(str(proj)) is True
    assert projectenv.project_env_for(str(proj / "a.py")) == str(proj)
    assert projectenv.dependencies_of(str(proj)) == ["cowsay", "altair"]


def test_unparseable_manifest_is_not_an_environment(home):
    proj = home / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("this is not [ valid toml", encoding="utf-8")

    assert projectenv.has_project_env(str(proj)) is False
    assert projectenv.dependencies_of(str(proj)) == []


def test_a_project_table_with_no_dependencies_is_not_an_environment(home):
    """A `uv init` scaffold must not force an empty venv.

    `[project]` with no dependencies declares nothing to install, so building a
    venv for it is all cost and no benefit — and worse than neutral: the venv is
    EMPTY, so a script that worked on the app interpreter (numpy, pandas, duckdb,
    geopandas, the whole bundled stack) fails on its first import. The pre-flight
    also renders the empty list as "…are not installed yet: . They need a
    one-time download."

    An empty declaration is PY-17: run on the app's own interpreter.
    """
    proj = home / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname='x'\nversion='1'\n", encoding="utf-8")
    (proj / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert projectenv.has_project_env(str(proj)) is False
    assert projectenv.dependencies_of(str(proj)) == []
    assert projectenv.project_env_for(str(proj / "a.py")) is None
    # The BOUNDARY is still that folder — it is a project, it just has no
    # environment of its own.
    assert projectenv.project_root_for(str(proj / "a.py")) == str(proj)


def test_an_explicitly_empty_dependency_list_is_not_an_environment(home):
    proj = _write_project(home / "proj", [])
    (proj / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert projectenv.has_project_env(str(proj)) is False
    assert projectenv.project_env_for(str(proj / "a.py")) is None


def test_a_dependency_whose_marker_excludes_this_platform_is_not_an_environment(home):
    """Nothing to install HERE means nothing to build here.

    A folder whose only dependency is `; sys_platform == 'darwin'` has an empty
    resolved list on Linux, and building an empty venv for it would take the
    script off the app interpreter for no gain — the same trap as an empty
    `[project]` table, reached by a different route.
    """
    proj = _write_project(home / "proj", ["python-pptx; sys_platform == 'never'"])
    (proj / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert projectenv.applicable_dependencies_of(str(proj)) == []
    assert projectenv.has_project_env(str(proj)) is False
    assert projectenv.project_env_for(str(proj / "a.py")) is None


# ---------------------------------------------------------------------------
# The venv key: the folder's absolute path, hashed as given
# ---------------------------------------------------------------------------


def test_venv_dir_is_under_the_home_dir_never_in_the_project(home, tmp_path):
    proj = _write_project(home / "proj")
    venv = projectenv.venv_dir_for(str(proj))

    assert venv.startswith(os.path.join(str(tmp_path / "home"), "venvs") + os.sep)
    assert not venv.startswith(str(proj) + os.sep)


def test_venv_key_is_the_sha256_of_the_absolute_path(home):
    proj = _write_project(home / "proj")
    expected = hashlib.sha256(str(proj).encode("utf-8")).hexdigest()[:16]
    assert projectenv.venv_key_for(str(proj)) == expected


def test_renaming_the_folder_gives_a_new_key(home):
    a = _write_project(home / "before")
    key_a = projectenv.venv_key_for(str(a))
    a.rename(home / "after")
    key_b = projectenv.venv_key_for(str(home / "after"))

    assert key_a != key_b


def test_key_is_not_canonicalised_through_a_symlink(home):
    """Two names for one directory key differently — move-means-reset depends on it."""
    real = _write_project(home / "real")
    link = home / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    assert projectenv.venv_key_for(str(link)) != projectenv.venv_key_for(str(real))


def test_key_is_stable_across_calls_and_relative_spellings(home, monkeypatch):
    proj = _write_project(home / "proj")
    monkeypatch.chdir(home)
    assert projectenv.venv_key_for("proj") == projectenv.venv_key_for(str(proj))


def test_uv_cache_dir_sits_beside_the_venvs(home, tmp_path):
    """One filesystem, so uv hardlinks instead of silently copying."""
    assert os.path.dirname(projectenv.uv_cache_dir()) == os.path.dirname(
        projectenv.venvs_root()
    )
    assert projectenv.uv_cache_dir().startswith(str(tmp_path / "home"))


# ---------------------------------------------------------------------------
# Staleness: a digest of the declaration, not an mtime
# ---------------------------------------------------------------------------


def test_the_lock_is_not_part_of_the_digest(home):
    """`uv.lock` is an OUTPUT of `uv sync`, not an input to it.

    Folding it in would make the environment's own side effect a reason to
    rebuild the environment. The intended consequence, pinned here: a hand-edit
    to the lock alone does not trigger a resync — the lock is generated, the
    manifest is the declaration.
    """
    proj = _write_project(home / "proj")
    first = projectenv.state_digest(str(proj))

    (proj / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    assert projectenv.state_digest(str(proj)) == first

    (proj / "uv.lock").write_text("version = 22\n", encoding="utf-8")
    assert projectenv.state_digest(str(proj)) == first


def test_digest_tracks_the_manifest_even_under_a_lock(home):
    """The requirement the digest exists for.

    Hashing the lock instead (on the reasoning that it is the resolved truth)
    means a dependency ADDED to pyproject.toml changes nothing, the venv reads as
    fresh, no install is offered, and the run fails later on an ImportError. A
    user must never have to run `uv sync` by hand to fix that — doing so would
    create an in-folder .venv and diverge from the home-dir store. The cost is a
    resync for a comment edit, which is a fast no-op through uv's cache.
    """
    proj = _write_project(home / "proj", ["cowsay"])
    (proj / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    first = projectenv.state_digest(str(proj))

    _write_project(home / "proj", ["cowsay", "altair"])
    assert projectenv.state_digest(str(proj)) != first


def test_the_digest_is_memoised_on_a_stat_fingerprint(home):
    """`is_installed` runs this on every /api/run and a uv.lock can be megabytes,
    so the steady state must be two stats — but a real edit must still be seen.

    The stat tuple is only the cache-invalidation hint; the digest stays the
    authoritative signal, so the copy2/mtime problem the digest exists to avoid
    is untouched.
    """
    proj = _write_project(home / "proj", ["cowsay"])
    projectenv.reset_state_digest_cache()

    reads = []
    real_compute = projectenv._compute_state_digest
    try:
        projectenv._compute_state_digest = (
            lambda root: (reads.append(root), real_compute(root))[1]
        )
        first = projectenv.state_digest(str(proj))
        assert projectenv.state_digest(str(proj)) == first
        assert projectenv.state_digest(str(proj)) == first
        assert len(reads) == 1, f"re-hashed an unchanged project {len(reads)} times"

        _write_project(home / "proj", ["cowsay", "altair"])  # size and mtime move
        second = projectenv.state_digest(str(proj))
    finally:
        projectenv._compute_state_digest = real_compute

    assert second != first, "an edit was hidden by the memo"
    assert len(reads) == 2


def test_digest_tracks_the_manifest_without_a_lock(home):
    proj = _write_project(home / "proj", ["cowsay"])
    first = projectenv.state_digest(str(proj))
    _write_project(home / "proj", ["cowsay", "altair"])
    assert projectenv.state_digest(str(proj)) != first


def test_digest_is_not_an_mtime(home):
    """Touching the files without changing them must not invalidate the venv.

    core_templates' copytree uses copy2, so every release stamps a template's
    pyproject newer than its venv. An mtime chain would resync all of them.
    """
    proj = _write_project(home / "proj")
    (proj / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    first = projectenv.state_digest(str(proj))
    os.utime(proj / "uv.lock", (1, 1))
    os.utime(proj / "pyproject.toml", (1, 1))
    assert projectenv.state_digest(str(proj)) == first


def test_digest_of_a_projectless_dir_is_empty(home):
    d = home / "nothing"
    d.mkdir()
    assert projectenv.state_digest(str(d)) == ""


# ---------------------------------------------------------------------------
# The sidecar: what a built venv says it was built from
# ---------------------------------------------------------------------------


def test_sidecar_roundtrips_path_and_digest(home):
    proj = _write_project(home / "proj")
    venv = projectenv.venv_dir_for(str(proj))
    os.makedirs(venv, exist_ok=True)

    projectenv.write_sidecar(venv, str(proj), "deadbeef")
    got = projectenv.read_sidecar(venv)

    assert got == {"path": str(proj), "digest": "deadbeef"}


def test_sidecar_is_none_when_absent_or_corrupt(home):
    proj = _write_project(home / "proj")
    venv = projectenv.venv_dir_for(str(proj))
    assert projectenv.read_sidecar(venv) is None

    os.makedirs(venv, exist_ok=True)
    with open(os.path.join(venv, projectenv.SIDECAR_NAME), "w", encoding="utf-8") as f:
        f.write("{not json")
    assert projectenv.read_sidecar(venv) is None


def test_sidecar_lands_inside_the_venv_not_the_project(home):
    proj = _write_project(home / "proj")
    venv = projectenv.venv_dir_for(str(proj))
    os.makedirs(venv, exist_ok=True)
    projectenv.write_sidecar(venv, str(proj), projectenv.state_digest(str(proj)))

    assert os.path.exists(os.path.join(venv, projectenv.SIDECAR_NAME))
    assert not os.path.exists(os.path.join(str(proj), projectenv.SIDECAR_NAME))


# ---------------------------------------------------------------------------
# Leftover PEP 723 headers are inert
# ---------------------------------------------------------------------------

HEADER = '# /// script\n# dependencies = ["altair", "cowsay"]\n# ///\n'


def test_a_header_never_supplies_an_environment(home):
    """A leftover header buys nothing — and costs nothing.

    Nothing reads it and nothing reacts to it: the block is a comment, and the
    folder alone decides the environment. This pins the "buys nothing" half; the
    "costs nothing" half — that carrying one does not make the file fail — is
    pinned in tests/test_engine.py.
    """
    d = home / "app"
    d.mkdir()
    (d / "a.py").write_text(HEADER + "x = 1\n", encoding="utf-8")

    assert projectenv.project_env_for(str(d / "a.py")) is None
    assert projectenv.has_project_env(str(d)) is False


# ---------------------------------------------------------------------------
# GC: a moved folder orphans its venv by design, so something has to reclaim it
# ---------------------------------------------------------------------------


def _fake_venv(project_dir):
    venv = projectenv.venv_dir_for(project_dir)
    os.makedirs(venv, exist_ok=True)
    projectenv.write_sidecar(venv, project_dir, projectenv.state_digest(project_dir))
    return venv


def test_gc_reclaims_a_venv_whose_source_is_gone(home):
    proj = _write_project(home / "gone")
    venv = _fake_venv(str(proj))
    live = _write_project(home / "still-here")
    live_venv = _fake_venv(str(live))

    import shutil

    shutil.rmtree(proj)

    assert projectenv.gc() == 1
    assert not os.path.exists(venv)
    assert os.path.exists(live_venv), "gc took a venv whose project still exists"


def test_gc_leaves_a_venv_alone_when_the_whole_VOLUME_is_missing(home, tmp_path):
    """An unmounted external drive is not a deleted project.

    `gc()` runs unconditionally at server startup, so booting once with the drive
    detached would otherwise delete every project venv for that workspace — the
    user reconnects the disk and gets a full re-download of each. The signal is
    the difference between "this folder is gone" and "nothing along this path is
    reachable": a deleted project leaves its PARENT behind, an unmounted volume
    does not.
    """
    volume = tmp_path / "Volumes" / "BigDisk"
    proj = _write_project(volume / "work" / "app")
    venv = _fake_venv(str(proj))

    import shutil

    shutil.rmtree(tmp_path / "Volumes")  # the drive goes away entirely

    assert projectenv.gc() == 0, "an unmounted volume was treated as a deletion"
    assert os.path.exists(venv)


def test_gc_still_reclaims_when_only_the_project_folder_is_gone(home):
    """The guard must not swallow the case gc exists for: the parent survives."""
    proj = _write_project(home / "work" / "app")
    venv = _fake_venv(str(proj))

    import shutil

    shutil.rmtree(proj)  # the project is deleted; `work/` is still there

    assert projectenv.gc() == 1
    assert not os.path.exists(venv)


def test_gc_leaves_a_venv_with_no_sidecar_alone(home):
    """It may be an install in flight; deleting one under a running worker is
    worse than leaking it."""
    proj = _write_project(home / "proj")
    venv = projectenv.venv_dir_for(str(proj))
    os.makedirs(venv)

    assert projectenv.gc() == 0
    assert os.path.exists(venv)


def test_gc_on_an_empty_store_is_a_no_op(home):
    assert projectenv.gc() == 0


def test_a_renamed_folder_orphans_its_venv_and_gc_reclaims_it(home):
    """The move-means-reset feature and its cost, in one test."""
    proj = _write_project(home / "before")
    old_venv = _fake_venv(str(proj))

    proj.rename(home / "after")
    new_venv = projectenv.venv_dir_for(str(home / "after"))

    assert new_venv != old_venv, "a moved folder must get a fresh environment"
    assert projectenv.gc() == 1
    assert not os.path.exists(old_venv)


# ---------------------------------------------------------------------------
# The module stays cheap on the request path
# ---------------------------------------------------------------------------


def test_module_does_not_import_the_fused_engine():
    """projectenv is consulted on every /api/run; importing fused.* would cost
    a geopandas/pyproj import on the request path."""
    import inspect

    src = inspect.getsource(projectenv)
    assert "import fused\n" not in src
    assert "from fused." not in src
    assert "import fused." not in src
