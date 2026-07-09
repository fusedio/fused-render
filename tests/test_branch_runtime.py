import importlib
import os

import pytest

import fused_render._branch as _branch


def _reload_with_ref(ref: str):
    """Set FUSED_RENDER_BRANCH, reset the _branch cache, then reload the
    target runtime modules so their module-level constants re-resolve.
    """
    os.environ["FUSED_RENDER_BRANCH"] = ref
    _branch._CACHED_REF = None
    importlib.reload(_branch)

    import fused_render.server as server
    import fused_render.app as app
    import fused_render.cli as cli

    server = importlib.reload(server)
    app = importlib.reload(app)
    cli = importlib.reload(cli)
    return server, app, cli


@pytest.fixture(autouse=True)
def _restore_baseline(monkeypatch):
    yield
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)
    _branch._CACHED_REF = None
    importlib.reload(_branch)
    import fused_render.server
    import fused_render.app
    import fused_render.cli

    importlib.reload(fused_render.server)
    importlib.reload(fused_render.app)
    importlib.reload(fused_render.cli)


def test_baseline_ref_empty(monkeypatch):
    server, app, cli = _reload_with_ref("")
    import fused_render.shell.storage as storage

    # Baseline: shell home is un-nested; templates live under it (D76).
    base = os.environ["FUSED_RENDER_HOME"]
    assert storage.home_dir() == base
    assert server.USER_TEMPLATES_DIR == os.path.join(base, "templates")
    assert server.USER_REGISTRY == os.path.join(base, "templates", "registry.json")
    assert app.APP_SUPPORT_DIR == os.path.expanduser(
        "~/Library/Application Support/fused-render"
    )
    assert app.DEFAULT_PORT == 8765
    assert app.MAX_PORT == 8775
    assert cli.DEFAULT_PORT == 8765


def test_branch_ref_foo(monkeypatch):
    server, app, cli = _reload_with_ref("foo")
    import fused_render.shell.storage as storage

    # Ref "foo": the whole shell home nests under foo/ (templates, bookmarks,
    # prefs all follow home_dir), and App Support + port shift too.
    base = os.environ["FUSED_RENDER_HOME"]
    assert storage.home_dir() == os.path.join(base, "foo")
    assert server.USER_TEMPLATES_DIR == os.path.join(base, "foo", "templates")
    assert app.APP_SUPPORT_DIR.endswith("Application Support/fused-render/foo")
    assert app.DEFAULT_PORT == _branch.branch_port("foo")
    assert app.MAX_PORT == app.DEFAULT_PORT + 10
    assert cli.DEFAULT_PORT == app.DEFAULT_PORT
