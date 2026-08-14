"""The built-in executor refuses a folder that declares its own environment.

The built-in executor spawns `[sys.executable, _child.py]` and has no venv
machinery at all — only the fused engine can build a project environment
(SPEC PY-16/PY-18, D174). That was harmless while `[bundled]` carried
everything the core templates import: a declaration was redundant under this
engine, and ignoring it cost nothing.

D275 ended that. `map`, `vector`, `geometry_editor` and `pdf_studio` now import
distributions no app interpreter ships, so under this engine their scripts spawn
and die on a bare `ModuleNotFoundError` from inside a tile request — a message
that names a package but not the reason, in a packaged app where `pip install`
is not something the user can do (D176's defect all over again). Three reachable
paths, all of which worked before D275:

  1. Preferences -> engine = builtin, a first-class UI toggle;
  2. `pip install "fused-render[bundled]"` on Python 3.10, where the `fused`
     requirement's `python_version >= "3.11"` marker skips the engine entirely;
  3. `FUSED_RENDER_ENGINE=builtin`.

So the executor now answers with the cause: which folder declared, what this
interpreter is missing, and the two ways to fix it. This file is the test that
did not exist — the whole gap was invisible because nothing exercised a
folder-manifest template under this engine.
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


def test_a_declared_environment_this_interpreter_cannot_meet_is_refused(tmp_path):
    """The regression D275 introduces, caught before the spawn.

    Without this the child starts, imports, and dies with
    `ModuleNotFoundError: No module named 'nonexistent_dist_...'` — true, and
    useless: it names neither the folder that asked for it nor the fact that the
    engine setting is what decides whether it can ever be installed.
    """
    path = _project(tmp_path, ["a-distribution-nobody-has-installed"])
    out = executor.run_python(path, {})

    assert out["ok"] is False
    assert out["error"]["type"] == "RuntimeError"
    message = out["error"]["message"]
    assert "pyproject.toml" in message
    assert os.path.basename(str(tmp_path)) in message
    assert "a-distribution-nobody-has-installed" in message
    # Both fixes must be reachable from the message alone.
    assert "Preferences" in message and "fused-render[fused]" in message


def test_a_declaration_this_interpreter_already_meets_still_runs(tmp_path):
    """Refuse the unmeetable, not the merely declared.

    A folder may declare something the app interpreter happens to have — every
    declaration is the COMPLETE list (D172), so `pandas` appears in manifests
    whose only unmet entry is something else. Under this engine such a script has
    always run correctly on the app interpreter, and turning that into a hard
    error would break working templates (`model_card`'s optional tokenizers path
    among them) to fix a problem they do not have. The refusal is therefore
    keyed on what is MISSING, not on the existence of a manifest.
    """
    path = _project(tmp_path, ["pytest"])  # certainly installed: it is running
    out = executor.run_python(path, {})
    assert out["ok"] is True, out.get("error")
    assert out["result"] == 1


def test_a_folder_with_no_manifest_is_untouched(tmp_path):
    """PY-17 is the common case and must stay free of all of this."""
    script = tmp_path / "plain.py"
    script.write_text("def main():\n    return 2\n", encoding="utf-8")
    out = executor.run_python(str(script), {})
    assert out["ok"] is True, out.get("error")
    assert out["result"] == 2


@pytest.mark.parametrize("folder", _declaring_folders())
def test_every_declaring_core_template_is_recognised_under_this_engine(folder):
    """The real declarations, checked without spawning the real templates.

    Driving `run_python` at `map/map_render.py` would either execute the Map
    Viewer (green suite, wrong reason) or depend on what the dev venv happens to
    have. The refusal decision is the thing under test, so it is asked directly —
    and asked about the SHIPPED manifests, because a copy of one in tmp_path
    would only prove the test fixture works.

    `projectenv` treats an immediate child of a template root as a project root
    and `PACKAGE_TEMPLATES_DIR` is one of those roots, so these paths resolve to
    the folder itself with no staging needed.
    """
    entry = os.path.join(_TEMPLATES, folder, "pyproject.toml")
    assert projectenv.project_env_for(entry) == os.path.join(_TEMPLATES, folder), (
        f"{folder} ships a pyproject.toml but projectenv does not resolve it as "
        "the project root — the refusal below could never fire for it"
    )
    refusal = executor.project_env_refusal(os.path.join(_TEMPLATES, folder, "x.py"))
    missing = projectenv.missing_from_this_interpreter(
        os.path.join(_TEMPLATES, folder)
    )
    if missing:
        assert refusal and folder in refusal and missing[0] in refusal
    else:
        assert refusal is None
