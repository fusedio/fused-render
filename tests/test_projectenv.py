"""A .py's project folder, and that folder's central venv (fused_render/projectenv.py).

The environment a script runs in is decided by the folder it belongs to, not by
anything in the file. That makes the boundary rule the whole contract: get it
wrong and two scripts in one app silently run in two environments, or a stray
manifest three levels down hijacks the app's.

Isolation: every test points FUSED_RENDER_HOME and FUSED_RENDER_DIR at tmp_path,
so the real ~/.fused-render and ~/Fused are never touched — the same
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


def test_nested_pyproject_with_no_applicable_deps_is_ignored(home, tmp_path):
    """A stray manifest below the root that declares nothing installable must
    not shadow the app's own — it is not a "real" project, just a `uv init`
    scaffold or `[tool.*]`-only file, and must not start demanding an empty
    venv."""
    app = tmp_path / "workspace" / "tag" / "my-app"
    _write_project(app, ["cowsay"])
    sub = app / "readers"
    _write_project(sub, [])
    (sub / "tiff.py").write_text("x = 1\n", encoding="utf-8")

    assert projectenv.project_root_for(str(sub / "tiff.py")) == str(app)
    assert projectenv.dependencies_of(str(app)) == ["cowsay"]


def test_nested_project_with_real_deps_becomes_the_boundary(home, tmp_path):
    """A folder nested below the app dir that genuinely declares its own
    environment (a real `[project]` table plus an applicable dependency) is
    the true boundary — not the app dir capped at <tag>/<name>. This is the
    background-app-engine case: an app dir that is itself just a container
    two levels deep, with the real project living one level further in."""
    app = tmp_path / "workspace" / "tag" / "my-app"
    app.mkdir(parents=True)
    sub = app / "OpenWhisper"
    _write_project(sub, ["pyobjc"])
    (sub / "menubar.py").write_text("x = 1\n", encoding="utf-8")

    assert projectenv.project_root_for(str(sub / "menubar.py")) == str(sub)
    assert projectenv.project_env_for(str(sub / "menubar.py")) == str(sub)


def test_app_dir_still_wins_when_it_also_declares_an_env(home, tmp_path):
    """When both the app dir and a nested folder declare real environments,
    the TOPMOST one wins (the app dir), matching the ancestor-walk rule
    further down this function: an inner manifest cannot shadow the outer
    one it sits inside."""
    app = tmp_path / "workspace" / "tag" / "my-app"
    _write_project(app, ["cowsay"])
    sub = app / "OpenWhisper"
    _write_project(sub, ["pyobjc"])
    (sub / "menubar.py").write_text("x = 1\n", encoding="utf-8")

    assert projectenv.project_root_for(str(sub / "menubar.py")) == str(app)


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


def test_stray_pyproject_at_the_ceiling_is_not_the_boundary_for_a_loose_script(home, tmp_path):
    """A script sitting directly in a tag folder — `<workspace>/<tag>/script.py`,
    no <name> level below it — makes `app_dir_for` return the FILE itself as a
    stand-in "app dir" rather than a directory. `start` (the tag folder) is
    then not even an ancestor of `app`, so the nested-env walk in
    `project_root_for` must not try to climb from it up to `app`: `d == app`
    would never fire, and without a ceiling check the walk would run past the
    ceiling to the filesystem root — exactly the failure `_ceiling()` exists
    to prevent, applied to a stray manifest at the shell home's parent.
    """
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='stray'\nversion='0.1'\ndependencies=['numpy']\n",
        encoding="utf-8",
    )
    loose = tmp_path / "workspace" / "tag" / "loose.py"
    loose.parent.mkdir(parents=True)
    loose.write_text("x = 1\n", encoding="utf-8")

    # Preserving prior (pre-nested-walk) behavior for this edge case: the
    # bogus file-as-app-dir is returned as-is, not the ceiling directory.
    assert projectenv.project_root_for(str(loose)) == str(loose)
    assert projectenv.project_env_for(str(loose)) is None


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


def test_venv_dir_is_in_the_project_by_default(home):
    """A writable folder of the user's gets the layout every other Python tool
    already expects — `uv run`, VS Code, the notebook kernel picker's own
    `.venv` walk — rather than a hash under the home dir."""
    proj = _write_project(home / "proj")
    assert projectenv.venv_dir_for(str(proj)) == str(proj / ".venv")


def test_FUSED_RENDER_VENV_IN_TREE_0_forces_the_home_store(home, tmp_path, monkeypatch):
    """The escape hatch: a cloud-synced or network-mounted project folder would
    otherwise sync a multi-gigabyte venv along with the user's files."""
    monkeypatch.setenv("FUSED_RENDER_VENV_IN_TREE", "0")
    proj = _write_project(home / "proj")
    venv = projectenv.venv_dir_for(str(proj))

    assert venv.startswith(os.path.join(str(tmp_path / "home"), "venvs") + os.sep)
    assert not venv.startswith(str(proj) + os.sep)


def test_a_folder_inside_the_package_resolves_to_the_home_store(home):
    """The packaged app's own tree is read-only in the shapes it ships in (the
    AppImage's squashfs mount, a Windows `Program Files` install) — reused from
    `_venv_identity`'s existing package-relative case rather than a second
    derivation of "is this in the package"."""
    runner = os.path.join(projectenv._PACKAGE_DIR, "ai", "runners", "faster_whisper")
    venv = projectenv.venv_dir_for(runner)

    assert venv == os.path.join(projectenv.venvs_root(), projectenv.venv_key_for(runner))
    assert not venv.startswith(runner + os.sep)


def test_a_non_writable_project_folder_falls_back_to_the_home_store(home):
    if os.name == "nt":
        pytest.skip("chmod does not model Windows ACL denial the way this probe reads it")
    if os.geteuid() == 0:
        pytest.skip("root writes through a permission-denied directory")
    proj = _write_project(home / "proj")
    os.chmod(proj, 0o500)
    try:
        venv = projectenv.venv_dir_for(str(proj))
    finally:
        os.chmod(proj, 0o700)  # so pytest can clean up the tmp dir

    assert venv == os.path.join(projectenv.venvs_root(), projectenv.venv_key_for(str(proj)))


def test_the_writability_answer_is_memoised(home, monkeypatch):
    """One probe per project per process — `venv_dir_for` runs on the
    `/api/run` pre-flight path, and an unmemoised create-exclusive probe per
    request is filesystem churn on the hot path."""
    proj = _write_project(home / "proj")
    probes = []
    real_probe = projectenv._probe_writable

    def _counting(path):
        probes.append(path)
        return real_probe(path)

    monkeypatch.setattr(projectenv, "_probe_writable", _counting)

    projectenv.venv_dir_for(str(proj))
    projectenv.venv_dir_for(str(proj))
    assert len(probes) == 1

    projectenv.reset_writable_cache()
    projectenv.venv_dir_for(str(proj))
    assert len(probes) == 2


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


def test_uv_cache_dir_is_isolated_under_an_explicit_FUSED_RENDER_HOME(home, tmp_path):
    """The one thing worth keeping from the old, always-explicit design: the
    test suite (via the `home` fixture, here and everywhere else) sets
    `FUSED_RENDER_HOME` precisely so a build under test can never reach a
    developer's real, shared uv cache. That isolation must survive exactly —
    renamed and re-documented from `test_uv_cache_dir_sits_beside_the_venvs`,
    which pinned this same call as the UNCONDITIONAL behaviour it used to be.
    """
    assert os.path.dirname(projectenv.uv_cache_dir()) == os.path.dirname(
        projectenv.venvs_root()
    )
    assert projectenv.uv_cache_dir().startswith(str(tmp_path / "home"))


def test_uv_cache_dir_defers_to_uvs_own_default_without_FUSED_RENDER_HOME(monkeypatch):
    """The new contract's other half. Per-branch cache fragmentation (three
    worktrees on one machine each holding their own multi-gigabyte torch
    download while `~/.cache/uv` already had it, on the SAME filesystem) came
    from `uv_cache_dir()` being unconditional, not from a decision that it
    should be. Outside test isolation, this must answer None — the cue
    `_env_install_worker._build` uses to leave `UV_CACHE_DIR` unset entirely
    and let uv resolve its own platform default.
    """
    monkeypatch.delenv("FUSED_RENDER_HOME", raising=False)
    assert projectenv.uv_cache_dir() is None


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
    """A home-store venv for *project_dir*, sidecar written and ready.

    Built at `<venvs_root>/<key>` directly rather than through `venv_dir_for` —
    these tests are about the orphan-reclaim mechanics of the home store itself
    (a project's parent surviving its own deletion, an unplugged volume, a
    missing sidecar), which is a question about that store regardless of
    whether `venv_dir_for` would place a FRESH venv there today. Going through
    `venv_dir_for` would put the venv inside `project_dir` for the writable tmp
    dirs these tests use, so deleting the project would delete the "orphan"
    along with it and the test would assert nothing.
    """
    venv = os.path.join(projectenv.venvs_root(), projectenv.venv_key_for(project_dir))
    os.makedirs(venv, exist_ok=True)
    projectenv.write_sidecar(venv, project_dir, projectenv.state_digest(project_dir))
    return venv


def test_gc_reclaims_a_venv_whose_source_is_gone(home, monkeypatch):
    # The escape hatch pins "still-here"'s venv to the home store too, so its
    # survival below tests the GONE-vs-ALIVE distinction this test is about
    # rather than tripping the separate RELOCATED arm — both live folders are
    # writable tmp dirs, and without this `venv_dir_for` would already agree
    # "still-here"'s venv belongs at `<still-here>/.venv`, making it relocated
    # (and reclaimed) regardless of what this test does.
    monkeypatch.setenv("FUSED_RENDER_VENV_IN_TREE", "0")
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
# GC: a venv the home store still holds after policy moved it in-tree
# ---------------------------------------------------------------------------


def test_gc_reclaims_a_home_store_venv_whose_project_now_resolves_in_tree(home):
    """A venv built back when everything lived under the home store, for a
    project that is a plain writable folder today — `venv_dir_for` no longer
    agrees this is where its venv belongs, and nothing else will ever read a
    directory `venv_dir_for` does not point at."""
    proj = _write_project(home / "proj")
    stray = _fake_venv(str(proj))

    assert projectenv.venv_dir_for(str(proj)) != stray, (
        "the fixture is vacuous unless policy actually disagrees with the venv's "
        "current location"
    )
    assert projectenv.gc() == 1
    assert not os.path.exists(stray)


def test_gc_keeps_a_home_store_venv_whose_source_folder_is_unreachable(home, tmp_path):
    """The dangerous case: an unplugged external drive must not be read as
    'relocated' and reclaimed.

    It falls out of the writability probe rather than needing its own check —
    an unreachable folder cannot be probed, so `venv_dir_for` answers the home
    store for it too, and a home-store venv is never "relocated" out from under
    itself. Getting this backwards would delete a multi-gigabyte venv the
    instant its volume is disconnected, on every single `gc()` at startup.
    """
    volume = tmp_path / "Volumes" / "BigDisk"
    proj = _write_project(volume / "work" / "app")
    venv = _fake_venv(str(proj))

    import shutil

    shutil.rmtree(tmp_path / "Volumes")  # the drive goes away entirely

    assert projectenv.gc() == 0, "an unreachable volume was treated as relocated"
    assert os.path.exists(venv)


# ---------------------------------------------------------------------------
# Renaming an in-tree project keeps its venv (the point of moving it in-tree)
# ---------------------------------------------------------------------------


def test_renaming_an_in_tree_project_keeps_pointing_at_the_same_venv(home):
    """The venv travels with the folder because it is INSIDE the folder — a
    rename that keeps the folder intact keeps `.venv` with it, unlike the
    home-store case where the key is derived from the path and a rename
    orphans it (see `test_a_renamed_folder_orphans_its_venv_and_gc_reclaims_it`,
    which is the same fact for that path)."""
    proj = _write_project(home / "before")
    venv = projectenv.venv_dir_for(str(proj))
    os.makedirs(venv)
    marker = os.path.join(venv, "some-file")
    open(marker, "w").close()

    proj.rename(home / "after")

    moved_venv = projectenv.venv_dir_for(str(home / "after"))
    assert moved_venv == str(home / "after" / ".venv")
    assert os.path.exists(os.path.join(moved_venv, "some-file")), (
        "the venv did not travel with its folder"
    )


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


# --- a project folder that ships INSIDE the app -------------------------------


def test_a_bundled_folder_is_keyed_on_its_place_in_the_package(home, monkeypatch):
    """Not on the app's own path, which is not stable on the packaged builds.

    The AppImage mounts itself at a fresh `.mount_FusedRxxxxxx` on every launch,
    so an absolute-path key gave the bundled AI runner folders a new venv key each
    time the app started: the multi-gigabyte environment built last launch was
    still on disk, still correct, and unreachable — a full re-download per launch,
    with `gc()` unable to reclaim the orphan (a vanished mount reads as merely
    unreachable, which it deliberately keeps).
    """
    first = home / ".mount_FusedRaaaaaa" / "fused_render"
    second = home / ".mount_FusedRbbbbbb" / "fused_render"
    runner = "ai/runners/faster_whisper"

    monkeypatch.setattr(projectenv, "_PACKAGE_DIR", str(first))
    key_first = projectenv.venv_key_for(str(first / runner))
    monkeypatch.setattr(projectenv, "_PACKAGE_DIR", str(second))
    key_second = projectenv.venv_key_for(str(second / runner))

    assert key_first == key_second


def test_two_bundled_folders_still_get_two_keys(home, monkeypatch):
    """Relativising is about the app's path moving, not about merging runners."""
    pkg = home / "fused_render"
    monkeypatch.setattr(projectenv, "_PACKAGE_DIR", str(pkg))

    whisper = projectenv.venv_key_for(str(pkg / "ai" / "runners" / "faster_whisper"))
    image = projectenv.venv_key_for(str(pkg / "ai" / "runners" / "diffusers_image"))

    assert whisper != image


def test_a_users_folder_beside_the_package_is_keyed_on_its_path(home, monkeypatch):
    """Only what is genuinely UNDER the package is bundled.

    `..`-escaping relative paths are what a naive relpath check lets through, and
    a user folder that keyed as though it were part of the app would collide with
    a real runner on the next release that added one.
    """
    pkg = home / "app" / "fused_render"
    monkeypatch.setattr(projectenv, "_PACKAGE_DIR", str(pkg))
    outside = _write_project(home / "app" / "mine")

    assert projectenv.venv_key_for(str(outside)) == hashlib.sha256(
        str(outside).encode("utf-8")
    ).hexdigest()[:16]


def test_the_real_runner_folders_key_as_bundled():
    """The wiring, not a stand-in: these are the folders the failure was about."""
    runner = os.path.join(projectenv._PACKAGE_DIR, "ai", "runners", "faster_whisper")
    assert projectenv._venv_identity(runner) == "<fused_render>/ai/runners/faster_whisper"


# --- the manifest mirror a read-only project's sync runs in -------------------


def test_the_worker_and_this_module_agree_on_the_mirror_suffix():
    """`_env_install_worker` cannot import this module (D152), so it restates it.

    A divergence would leak a mirror per reclaimed venv, each holding the lock its
    environment was resolved from.
    """
    import importlib.util
    from pathlib import Path

    path = Path(projectenv.__file__).with_name("_env_install_worker.py")
    spec = importlib.util.spec_from_file_location("_worker_mirror_suffix", path)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)

    assert worker._MIRROR_SUFFIX == projectenv.MIRROR_SUFFIX


def test_gc_reclaims_a_venvs_mirror_with_it(home):
    """The mirror is a sibling of the venv with no sidecar of its own.

    Left behind it is a permanent orphan — `gc` only ever looks at directories
    that can say what they were built from, and a mirror cannot.
    """
    gone = home / "deleted"
    gone.mkdir()
    venv = os.path.join(projectenv.venvs_root(), projectenv.venv_key_for(str(gone)))
    os.makedirs(venv)
    projectenv.write_sidecar(venv, str(gone), "digest")
    mirror = venv + projectenv.MIRROR_SUFFIX
    os.makedirs(mirror)
    open(os.path.join(mirror, "uv.lock"), "w").close()
    gone.rmdir()

    assert projectenv.gc() == 1
    assert not os.path.exists(venv)
    assert not os.path.exists(mirror), "the mirror outlived the venv it belonged to"


def test_gc_never_reclaims_a_mirror_on_its_own_account(home, monkeypatch):
    """A mirror beside a LIVE venv is holding that venv's lock."""
    # Pins the venv to the home store: "live" is a writable tmp dir, and this
    # test is about the mirror-beside-a-venv rule, not the RELOCATED arm.
    monkeypatch.setenv("FUSED_RENDER_VENV_IN_TREE", "0")
    proj = _write_project(home / "live")
    venv = os.path.join(projectenv.venvs_root(), projectenv.venv_key_for(str(proj)))
    os.makedirs(venv)
    projectenv.write_sidecar(venv, str(proj), "digest")
    mirror = venv + projectenv.MIRROR_SUFFIX
    os.makedirs(mirror)

    assert projectenv.gc() == 0
    assert os.path.isdir(mirror)


def test_gc_reclaims_a_mirror_that_never_GOT_a_venv(home):
    """A build can leave a mirror and no venv, and nothing else would ever look.

    `uv sync` creates the mirror before it resolves, so a resolver failure — no
    wheel for this platform, no network, a bad pin — leaves one behind with no venv
    beside it. So does a project deleted between the sync starting and finishing.
    A mirror has no sidecar, so the main loop skips it, and reclaiming it with its
    venv cannot help when there is no venv: it would sit there for the life of the
    install, once per failed attempt.
    """
    proj = _write_project(home / "never-built")
    venv = os.path.join(projectenv.venvs_root(), projectenv.venv_key_for(str(proj)))
    mirror = venv + projectenv.MIRROR_SUFFIX
    os.makedirs(mirror)
    open(os.path.join(mirror, "pyproject.toml"), "w").close()

    # Not counted: the number gc returns is what startup logs as reclaimed VENVS.
    assert projectenv.gc() == 0
    assert not os.path.exists(mirror)


def test_gc_reclaims_a_BUNDLED_venv_whose_runner_folder_is_gone(home, monkeypatch):
    """The reason the sidecar records the identity and not the mount path.

    An AppImage mounts itself somewhere new on every launch, so an absolute path
    recorded for a folder inside it names a directory that will never exist again —
    and `gc` deliberately KEEPS a venv whose source is merely unreachable, because
    that is also what an unplugged external drive looks like. A runner folder that
    a release removes or renames would therefore strand its multi-gigabyte
    environment permanently. Recording `<fused_render>/ai/runners/…` and resolving
    it against the package dir of THIS launch is what makes the deletion visible.
    """
    first = home / ".mount_FusedRaaaaaa" / "fused_render"
    second = home / ".mount_FusedRbbbbbb" / "fused_render"
    (second / "ai" / "runners").mkdir(parents=True)  # the runner itself is gone
    runner = _write_project(first / "ai" / "runners" / "faster_whisper")

    monkeypatch.setattr(projectenv, "_PACKAGE_DIR", str(first))
    venv = projectenv.venv_dir_for(str(runner))
    os.makedirs(venv)
    projectenv.write_sidecar(venv, str(runner), "digest")

    monkeypatch.setattr(projectenv, "_PACKAGE_DIR", str(second))
    assert projectenv.gc() == 1
    assert not os.path.exists(venv)


def test_gc_keeps_a_BUNDLED_venv_whose_runner_the_new_mount_still_has(home, monkeypatch):
    """The half that must not break: a remount is not a deletion.

    Every launch of the AppImage is a new mount directory, so if resolving the
    identity read as "gone" the very first `gc` after any restart would delete
    every runner environment on the machine.
    """
    first = home / ".mount_FusedRaaaaaa" / "fused_render"
    second = home / ".mount_FusedRbbbbbb" / "fused_render"
    runner = _write_project(first / "ai" / "runners" / "faster_whisper")
    _write_project(second / "ai" / "runners" / "faster_whisper")

    monkeypatch.setattr(projectenv, "_PACKAGE_DIR", str(first))
    venv = projectenv.venv_dir_for(str(runner))
    os.makedirs(venv)
    projectenv.write_sidecar(venv, str(runner), "digest")

    import shutil

    shutil.rmtree(first)  # last launch's mount is long gone
    monkeypatch.setattr(projectenv, "_PACKAGE_DIR", str(second))

    assert projectenv.gc() == 0
    assert os.path.isdir(venv)


def test_gc_still_reads_a_sidecar_written_the_OLD_way(home, monkeypatch):
    """Installed copies have absolute-path sidecars on disk right now.

    The identity is deliberately unspellable as a path, so a recorded path can
    never be mistaken for one — an existing sidecar keeps exactly the behaviour it
    had, which for a user's folder is the full rule and for a bundled one is
    "never reclaimed until the next rebuild rewrites it". The failure to avoid in
    both directions: a venv that becomes uncollectable, or one collected while its
    source is alive.
    """
    # "live" is a writable tmp dir, so without the escape hatch its home-store
    # venv would be RELOCATED (policy now wants `<live>/.venv`) and reclaimed
    # for that reason — which is correct but is not what this test pins.
    monkeypatch.setenv("FUSED_RENDER_VENV_IN_TREE", "0")
    import json

    def _old_style_sidecar(folder):
        venv = os.path.join(projectenv.venvs_root(),
                            projectenv.venv_key_for(str(folder)))
        os.makedirs(venv)
        with open(os.path.join(venv, projectenv.SIDECAR_NAME), "w", encoding="utf-8") as fh:
            json.dump({"path": str(folder), "digest": "d"}, fh)
        return venv

    gone = home / "gone"
    gone.mkdir()
    venv = _old_style_sidecar(gone)
    live_venv = _old_style_sidecar(_write_project(home / "live"))
    gone.rmdir()

    assert projectenv.gc() == 1
    assert not os.path.exists(venv)
    assert os.path.isdir(live_venv)


def test_the_worker_and_this_module_agree_on_the_recorded_IDENTITY(home, monkeypatch):
    """Both write the sidecar, and `gc` resolves what they wrote.

    `_env_install_worker` cannot import this module (D152), so it restates the
    computation. A divergence gives a bundled venv a sidecar `gc` cannot map back
    to a folder — which is precisely the permanent multi-gigabyte orphan the
    package-relative identity exists to prevent, reintroduced by the mechanism
    meant to fix it.
    """
    import importlib.util
    from pathlib import Path

    path = Path(projectenv.__file__).with_name("_env_install_worker.py")
    spec = importlib.util.spec_from_file_location("_worker_identity", path)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)

    pkg = home / ".mount_FusedRaaaaaa" / "fused_render"
    monkeypatch.setattr(projectenv, "_PACKAGE_DIR", str(pkg))
    monkeypatch.setattr(worker, "_PACKAGE_DIR", str(pkg))

    for folder in (pkg / "ai" / "runners" / "faster_whisper",
                   pkg,
                   home / "a-users-folder",
                   home / ".mount_FusedRaaaaaa" / "beside-the-package"):
        assert worker._source_identity(str(folder)) == projectenv._venv_identity(str(folder))

    assert worker._PACKAGE_IDENTITY == projectenv._PACKAGE_IDENTITY


def test_the_worker_finds_the_package_dir_without_importing_the_package():
    """It restates the constant, but it must not restate the VALUE.

    The worker file lives in `fused_render/`, so its own dirname is the package
    dir — no argv slot and no import (D152). Hard-coding anything else would make
    the two agree in a test that monkeypatches both and disagree in production.
    """
    import importlib.util
    from pathlib import Path

    path = Path(projectenv.__file__).with_name("_env_install_worker.py")
    spec = importlib.util.spec_from_file_location("_worker_package_dir", path)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)

    assert worker._PACKAGE_DIR == projectenv._PACKAGE_DIR


def test_write_sidecar_records_a_bundled_folders_identity(home, monkeypatch):
    """The server-side writer of the same record the worker writes."""
    import json

    pkg = home / "fused_render"
    monkeypatch.setattr(projectenv, "_PACKAGE_DIR", str(pkg))
    runner = _write_project(pkg / "ai" / "runners" / "faster_whisper")
    venv = projectenv.venv_dir_for(str(runner))
    os.makedirs(venv)

    projectenv.write_sidecar(venv, str(runner), "digest")

    with open(os.path.join(venv, projectenv.SIDECAR_NAME), encoding="utf-8") as fh:
        assert json.load(fh)["path"] == "<fused_render>/ai/runners/faster_whisper"
