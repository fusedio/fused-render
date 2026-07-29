"""Staging of the packaged templates into ~/.fused-render/.core-templates
(fused_render/core_templates.py).

The server never reads `fused_render/templates/` directly — it reads the staged
copy. So the *staleness rule* of that copy is a correctness contract, not an
optimisation: if the marker says "already staged" when the packaged tree has
actually changed, every user keeps being served the previous release's
templates. That is exactly what happened to the theme work (12 templates edited,
`__version__` untouched, nothing re-staged), so the marker is content-sensitive:
`<version> <sha256 of the packaged tree>`.

Isolation: every test points FUSED_RENDER_HOME at tmp_path and (where it needs
to mutate the source) PACKAGE_TEMPLATES_DIR at a tiny fake tree, so the real
~/.fused-render and the real package are never touched.
"""
import os

import pytest
from fastapi.testclient import TestClient

from fused_render import core_templates
from fused_render.core_templates import ensure_core_templates
from fused_render.server import create_app


@pytest.fixture
def staged(tmp_path, monkeypatch):
    """A throwaway home + a tiny fake packaged template tree."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(core_templates._OVERRIDE_ENV, raising=False)
    pkg = tmp_path / "pkg-templates"
    (pkg / "csv").mkdir(parents=True)
    (pkg / "csv" / "template.html").write_text("<html>dark only</html>", encoding="utf-8")
    (pkg / "registry.json").write_text('{".csv": ["csv"]}', encoding="utf-8")
    monkeypatch.setattr(core_templates, "PACKAGE_TEMPLATES_DIR", str(pkg))

    class Staged:
        package = pkg

        @property
        def core(self):
            return core_templates.core_templates_dir()

        def marker(self):
            with open(os.path.join(self.core, ".version"), encoding="utf-8") as f:
                return f.read().strip()

        def served(self, name="csv"):
            with open(os.path.join(self.core, name, "template.html"), encoding="utf-8") as f:
                return f.read()

    return Staged()


def test_editing_a_packaged_template_restages_without_a_version_bump(staged):
    ensure_core_templates()
    assert staged.served() == "<html>dark only</html>"

    # The exact shape of the theme bug: a template edit, no version change.
    (staged.package / "csv" / "template.html").write_text(
        '<html data-fused-theme>light + dark</html>', encoding="utf-8"
    )
    ensure_core_templates()
    assert staged.served() == '<html data-fused-theme>light + dark</html>'


def test_a_legacy_bare_version_marker_restages(staged):
    """Heals every install staged by the version-only marker logic."""
    from fused_render import __version__

    ensure_core_templates()
    core = staged.core
    # Simulate an install from before the digest: bare version, stale content.
    with open(os.path.join(core, "csv", "template.html"), "w", encoding="utf-8") as f:
        f.write("<html>stale</html>")
    with open(os.path.join(core, ".version"), "w", encoding="utf-8") as f:
        f.write(__version__)

    ensure_core_templates()
    assert staged.served() == "<html>dark only</html>"
    assert staged.marker() != __version__
    assert staged.marker().startswith(__version__ + " ")


def test_an_unchanged_tree_does_not_recopy(staged, monkeypatch):
    ensure_core_templates()

    def boom(*args, **kwargs):
        raise AssertionError("re-staged an unchanged packaged tree")

    monkeypatch.setattr(core_templates.shutil, "copytree", boom)
    monkeypatch.setattr(core_templates.shutil, "rmtree", boom)
    assert ensure_core_templates() == staged.core


def test_the_tree_digest_is_stable_and_order_independent(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    for root, order in ((a, ("z", "m", "a")), (b, ("a", "z", "m"))):
        for name in order:
            (root / name).mkdir(parents=True)
            (root / name / "template.html").write_text(f"<{name}>", encoding="utf-8")

    digest = core_templates._tree_digest(str(a))
    assert digest == core_templates._tree_digest(str(a)), "not stable across calls"
    assert digest == core_templates._tree_digest(str(b)), "creation order leaked in"

    (b / "a" / "template.html").write_text("<changed>", encoding="utf-8")
    assert core_templates._tree_digest(str(b)) != digest, "content change not observed"

    (a / "a" / "renamed.html").write_text("<a>", encoding="utf-8")
    os.remove(a / "a" / "template.html")
    assert core_templates._tree_digest(str(a)) != digest, "path change not observed"


def test_the_override_env_still_short_circuits(staged, monkeypatch):
    monkeypatch.setenv(core_templates._OVERRIDE_ENV, str(staged.package))
    assert ensure_core_templates() == str(staged.package)
    assert not os.path.exists(staged.core)


# ------------------------------------------------------------------ end to end


@pytest.fixture
def upgraded_install(tmp_path, monkeypatch):
    """A home staged by a PREVIOUS release, then re-entered by this one.

    This is the state every real install was in: `.core-templates/` holds the
    pre-theme templates under a bare-version `.version` marker, and the app
    starts again on the same version. Reproducing it is the whole point — the
    session's conftest home is staged fresh from the current package, so it can
    never show a staleness bug.

    Yields the core dir the server should read after `ensure_core_templates()`
    has had its say; `server.TEMPLATES_DIR` is repointed at it (the same
    module-constant seam test_templates_api.py uses for USER_TEMPLATES_DIR).
    """
    from fused_render import __version__
    from fused_render.server import templates as _server_templates

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(core_templates._OVERRIDE_ENV, raising=False)
    core = ensure_core_templates()

    # Age the staged copy back to before the appearance work: strip the opt-in
    # attribute from every template, and leave a bare-version marker behind.
    for name in os.listdir(core):
        page = os.path.join(core, name, "template.html")
        if not os.path.isfile(page):
            continue
        with open(page, encoding="utf-8") as f:
            old = f.read()
        with open(page, "w", encoding="utf-8") as f:
            f.write(old.replace("data-fused-theme", "data-pre-theme"))
    with open(os.path.join(core, ".version"), "w", encoding="utf-8") as f:
        f.write(__version__)

    monkeypatch.setattr(_server_templates, "USER_TEMPLATES_DIR", str(tmp_path / "no-overrides"))
    monkeypatch.setattr(_server_templates, "TEMPLATES_DIR", ensure_core_templates())
    return _server_templates.TEMPLATES_DIR


@pytest.mark.parametrize("template", ["text", "code"])
def test_render_serves_a_theme_aware_tier_one_template(tmp_path, upgraded_install, template):
    """The one test that reads what the server actually SENDS.

    Everything else about the appearance work (tests/test_theme.py) parses repo
    files, which is why a staging layer serving last release's copies went
    unnoticed. This goes through the real _resolve_name path and then /render,
    so it fails whenever the staged copy is stale.
    """
    from fused_render.server import templates as _server_templates

    path, error = _server_templates._resolve_name(template)
    assert error is None, error
    assert path.startswith(upgraded_install), (
        f"{template} must resolve to the staged core copy, not a user override"
    )

    client = TestClient(create_app(start_dir=str(tmp_path)))
    body = client.get("/render", params={"path": path}).text
    assert '<script src="/static/runtime.js"></script>' in body
    assert "data-fused-theme" in body
