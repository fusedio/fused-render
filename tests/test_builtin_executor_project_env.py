"""What the built-in executor does with a folder that declares an environment.

The built-in executor spawns `[sys.executable, _child.py]` and has no venv
machinery at all — only the fused engine can build a project environment
(SPEC PY-16/PY-18, D174). That was harmless while `[bundled]` carried everything
the core templates import: a declaration was redundant under this engine, and
ignoring it cost nothing.

D275 ended that. `map`, `vector`, `geometry_editor` and `pdf_studio` now import
distributions no app interpreter ships, so under this engine their scripts can
die on a bare `ModuleNotFoundError` from inside a tile request — a message that
names a package but not the reason, in a packaged app where `pip install` is not
something the user can do (D176's defect all over again). Three reachable paths,
all of which worked before D275:

  1. Preferences -> engine = builtin, a first-class UI toggle;
  2. `pip install "fused-render[bundled]"` on Python 3.10, where the `fused`
     requirement's `python_version >= "3.11"` marker skips the engine entirely;
  3. `FUSED_RENDER_ENGINE=builtin`.

**The fix explains, it does not refuse.** The first version of this file pinned a
pre-flight refusal keyed on the folder's state, and that was wrong at a
granularity the spec did not reveal: `docs`, `geotiff`, `latex`, `model_card`
and `pano` have each declared a heavy optional dependency for months while their
entry points stay stdlib-only ON PURPOSE — `geotiff`'s `ensure()`,
`model_card`'s `inspect_model.py`, whose manifest promises the card "renders
identically under either engine". A folder-scoped refusal took all five down,
and this file ASSERTED that it did, which is how a green suite came to encode a
regression. Both halves are fixed here: the run proceeds, and the explanation is
attached only where an import actually failed.

Every assertion below about a missing distribution uses SIMULATED absence. The
dev venv has the whole stack installed, so a test that asked the real
interpreter would take the "nothing is missing" branch and prove nothing — which
is precisely what the four D275 folders did in the version this replaces.
"""
import os

import pytest

from fused_render import executor, projectenv

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_REPO, "fused_render", "templates")


def _declaring_folders():
    return sorted(
        name for name in os.listdir(_TEMPLATES)
        if os.path.isfile(os.path.join(_TEMPLATES, name, "pyproject.toml"))
    )


def _absent(monkeypatch, *names):
    """Make `names` look uninstalled to `missing_from_this_interpreter`."""
    import importlib.metadata as md

    real = md.version

    def fake(dist):
        if projectenv._normalize_dist(dist) in {
            projectenv._normalize_dist(n) for n in names
        }:
            raise md.PackageNotFoundError(dist)
        return real(dist)

    monkeypatch.setattr(md, "version", fake)


def _project(tmp_path, deps, body="def main():\n    return 1\n"):
    """A folder that declares `deps`, with one script in it."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "p"\nversion = "0.1.0"\n'
        "dependencies = [" + ", ".join(f'"{d}"' for d in deps) + "]\n",
        encoding="utf-8",
    )
    script = tmp_path / "run.py"
    script.write_text(body, encoding="utf-8")
    return str(script)


# ------------------------------------------------------ the message, end to end


def test_a_declared_import_that_is_missing_here_explains_itself(tmp_path):
    """The D275 regression, explained at the point it actually breaks."""
    path = _project(
        tmp_path,
        ["a-distribution-nobody-has-installed"],
        body="import a_distribution_nobody_has_installed\n\n"
             "def main():\n    return 1\n",
    )
    out = executor.run_python(path, {})

    assert out["ok"] is False
    assert out["error"]["type"] == "ModuleNotFoundError"
    message = out["error"]["message"]
    assert "a_distribution_nobody_has_installed" in message
    assert "pyproject.toml" in message
    assert os.path.basename(str(tmp_path)) in message
    # Both fixes must be reachable from the message alone.
    assert "Preferences" in message and "fused-render[fused]" in message


def test_a_missing_module_nobody_declared_is_left_alone(tmp_path):
    """Blaming the environment for a typo is worse than saying nothing.

    The folder declares an environment AND has something missing from it, so
    every precondition except the important one holds — and the important one is
    that the failed import must be the thing the manifest asked for.
    """
    path = _project(
        tmp_path,
        ["a-distribution-nobody-has-installed"],
        body="import a_typo_the_user_made\n\ndef main():\n    return 1\n",
    )
    out = executor.run_python(path, {})

    assert out["ok"] is False
    assert out["error"]["message"] == "No module named 'a_typo_the_user_made'"


def test_a_folder_with_no_manifest_is_untouched(tmp_path):
    """PY-17 is the common case and must stay free of all of this."""
    script = tmp_path / "plain.py"
    script.write_text("import a_typo\n\ndef main():\n    return 2\n", encoding="utf-8")
    out = executor.run_python(str(script), {})
    assert out["error"]["message"] == "No module named 'a_typo'"


def test_a_declaring_folder_still_runs_when_nothing_actually_breaks(tmp_path):
    """The whole point of explaining instead of refusing.

    This is the shape of `geotiff`'s `ensure()`, `model_card`'s
    `inspect_model.py`, `pano`, `docs` and `latex`: a manifest naming something
    heavy, an entry point that never touches it. A pre-flight refusal failed
    every one of these; this must not.
    """
    path = _project(tmp_path, ["a-distribution-nobody-has-installed"])
    out = executor.run_python(path, {})
    assert out["ok"] is True, out.get("error")
    assert out["result"] == 1


def test_the_missing_module_pattern_matches_a_real_error():
    """Pin the message format against a genuinely raised error.

    The enrichment reads the module name out of the message, because neither
    execution path keeps the exception object. If CPython ever rewords this, the
    enrichment would silently stop firing and every test above would still pass
    on its "no match -> return None" branch — a check that quietly turns itself
    off is the failure this whole file exists to prevent.
    """
    with pytest.raises(ModuleNotFoundError) as raised:
        __import__("a_module_that_certainly_does_not_exist")
    found = executor._MISSING_MODULE.search(str(raised.value))
    assert found and found.group(1) == "a_module_that_certainly_does_not_exist"


def test_an_import_error_with_no_module_name_is_left_alone(tmp_path):
    """`ImportError` is not always "module missing" — a circular import and a
    failed `from x import y` both arrive with this type and no such phrase."""
    path = _project(tmp_path, ["a-distribution-nobody-has-installed"])
    assert executor.explain_missing_module(
        path, {"type": "ImportError", "message": "cannot import name 'z' from 'w'"}
    ) is None


def test_which_of_two_overlapping_messages_wins(tmp_path, monkeypatch):
    """`map/worker.py` writes its own richer message; both cases are pinned.

    `worker.py` raises `ModuleNotFoundError("No module named 'x'. A Python map
    target runs inside …")` when the user's own map target fails to import, and
    that message opens with the exact phrase this enrichment matches on. Which
    one the user sees depends on whether `x` is something map DECLARES:

      * **not declared** (`xarray`, `torch`, anything of the user's own) —
        worker's message survives, because the gate is the declaration and a
        module map never asked for is not this function's business;
      * **declared and absent here** (`duckdb` on a packaged app running the
        built-in engine, the real post-D275 state) — the enrichment REPLACES
        worker's message.

    The second is the interesting one and it was described wrongly before: the
    survival rule was stated as "it names an undeclared module", which is only
    what happens to be true on a dev venv where everything is installed. The
    replacement is deliberate and better — worker's message tells the user to
    rewrite their target, while in that state the actionable fact is that the
    engine setting is why the environment was never built. Losing worker's extra
    sentence about which process the target ran in is the accepted cost.
    """
    worker = os.path.join(_TEMPLATES, "map", "worker.py")
    worker_message = (
        "No module named '{}'. A Python map target runs inside the Map "
        "Viewer's own environment (…)"
    )

    _absent(monkeypatch, "duckdb")
    # Not declared by map -> worker keeps the floor.
    assert executor.explain_missing_module(
        worker, {"type": "ModuleNotFoundError",
                 "message": worker_message.format("xarray")},
    ) is None
    # Declared by map and absent here -> the engine explanation takes over.
    taken_over = executor.explain_missing_module(
        worker, {"type": "ModuleNotFoundError",
                 "message": worker_message.format("duckdb")},
    )
    assert taken_over and "Preferences" in taken_over


# ------------------------------------------------------- the real declarations


@pytest.mark.parametrize("folder", _declaring_folders())
def test_every_declaring_core_template_resolves_as_its_own_project(folder):
    """`projectenv` must see these folders, or nothing below can ever fire.

    `PACKAGE_TEMPLATES_DIR` is one of the template roots and an immediate child
    of a root is a project root, so these resolve with no staging needed.
    """
    entry = os.path.join(_TEMPLATES, folder, "reader.py")
    assert projectenv.project_env_for(entry) == os.path.join(_TEMPLATES, folder)


@pytest.mark.parametrize("folder", _declaring_folders())
def test_every_declaring_core_template_can_explain_its_own_dependency(
    folder, monkeypatch
):
    """Each shipped manifest's first dependency, simulated absent, is explained.

    Asked against SIMULATED absence rather than the real interpreter: on a dev
    venv with the whole stack installed every one of these would take the
    "nothing is missing" branch and assert nothing at all. That vacuity is not
    hypothetical — it is what the version of this file that this replaces did
    for `map`, `vector`, `pdf_studio` and `geometry_editor`.

    Also covers the module-name -> distribution step for every name the shipped
    manifests actually use, including `pypandoc-binary`, whose import name is
    `pypandoc` and which no reverse lookup could resolve on its own.
    """
    declared = projectenv.applicable_dependencies_of(os.path.join(_TEMPLATES, folder))
    assert declared, f"{folder} declares nothing; this parametrization is stale"
    dist = projectenv._normalize_dist(declared[0].split(";")[0].split(">")[0]
                                      .split("=")[0].split("[")[0].strip())
    module = next(
        (m for m, d in projectenv._MODULE_TO_DIST.items() if d == dist),
        dist.replace("-", "_"),
    )

    _absent(monkeypatch, dist)
    explanation = executor.explain_missing_module(
        os.path.join(_TEMPLATES, folder, "reader.py"),
        {"type": "ModuleNotFoundError", "message": f"No module named '{module}'"},
    )
    assert explanation and folder in explanation and dist in explanation


def _equirect_image(tmp_path):
    """A 2:1 image pano.py will accept, so its happy path is reachable."""
    pytest.importorskip("PIL")
    from PIL import Image

    path = tmp_path / "pano.jpg"
    Image.new("RGB", (64, 32), (10, 20, 30)).save(path)
    return {"action": "open", "file": str(path)}


# (folder, entry point, params). The five templates a folder-scoped refusal
# broke, each driven through the entry point and action its own manifest or
# docstring documents as the stdlib-only one. `params` is a callable so a case
# can build a fixture file; every action here is side-effect-free — a status
# probe or a read — and none of them downloads anything.
_STDLIB_ONLY_RUNS = [
    ("geotiff", "tiff_reader.py", lambda tmp: {}),
    ("model_card", "inspect_model.py", lambda tmp: {"path": str(tmp)}),
    ("pano", "pano.py", _equirect_image),
    ("docs", "docs.py", lambda tmp: {"action": "typst_status"}),
    ("latex", "engine.py", lambda tmp: {"action": "tectonic_status"}),
]


@pytest.mark.parametrize(
    "folder,entrypoint,params", _STDLIB_ONLY_RUNS,
    ids=[row[0] for row in _STDLIB_ONLY_RUNS],
)
def test_a_stdlib_only_template_really_runs_under_this_engine(
    folder, entrypoint, params, tmp_path, monkeypatch
):
    """The headline claim of this whole change, actually executed.

    These five predate D275 and each declares something heavy and optional —
    `imagecodecs`, `tokenizers`, `py360convert`, `pypandoc-binary` — while the
    entry point below stays stdlib-only on purpose. A folder-scoped pre-flight
    refusal took all five down, and the version of this test that replaced it
    only ever called `explain_missing_module`, so a refusal reinstated ANYWHERE
    ELSE in `run_python` left it green. It goes through the front door now.

    The condition is FORCED rather than borrowed from the machine: the parent is
    made to believe every one of the folder's declared distributions is absent —
    exactly the state that triggered the round-2 refusal — while the child
    subprocess runs for real against the actual interpreter. That makes the
    guarantee deterministic instead of a property of whatever this venv happens
    to have installed, which is what let the earlier version prove nothing.
    """
    root = os.path.join(_TEMPLATES, folder)
    declared = [
        projectenv._normalize_dist(
            d.split(";")[0].split(">")[0].split("=")[0].split("[")[0].strip())
        for d in projectenv.applicable_dependencies_of(root)
    ]
    _absent(monkeypatch, *declared)
    assert set(projectenv.missing_from_this_interpreter(root)) == set(declared), (
        "the simulated absence did not take, so this run would not reproduce the "
        "condition a pre-flight refusal fired on and would prove nothing"
    )

    out = executor.run_python(os.path.join(root, entrypoint), params(tmp_path))
    assert out["ok"] is True, (
        f"{folder}/{entrypoint} is a stdlib-only entry point that ran fine "
        f"before D275 and must keep running under the built-in executor however "
        f"much its folder declares — got {out.get('error')}"
    )
