"""The app-tool runner: what a served tool sees and what it hands back."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from fused_render.compute import app_mcp

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="posix_spawn path")

TOOLS = """\
import os


def where():
    return os.getcwd()


def json_text():
    return '{"a": 1}'


def plain_text():
    return "hello"


def a_dict():
    return {"a": 1}
"""

MANIFEST = "".join(
    f'[[tool]]\nname = "{n}"\nfile = "t.py"\nentrypoint = "{n}"\ndescription = "d"\n\n'
    for n in ("where", "json_text", "plain_text", "a_dict")
)


async def _fake_interpreter(spec):
    return sys.executable


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mcp, "_app_interpreter", _fake_interpreter)
    (tmp_path / "t.py").write_text(TOOLS)
    (tmp_path / "mcp.toml").write_text(MANIFEST)
    manifest = app_mcp.load_app_manifest(tmp_path)
    specs = {t.name: app_mcp.build_app_tool_spec(manifest, t) for t in manifest.tools}

    def call(name):
        return asyncio.run(app_mcp._run_app_tool(specs[name], {}))

    return call


def test_a_tool_runs_in_the_calls_context_dir_not_the_app_dir(served, tmp_path):
    cwd = Path(served("where"))
    assert cwd.name == "context"
    assert cwd.resolve() != tmp_path.resolve()


def test_a_tool_can_locate_the_app_through_file(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mcp, "_app_interpreter", _fake_interpreter)
    (tmp_path / "t.py").write_text(
        "import os\n\ndef main():\n    return os.path.dirname(os.path.abspath(__file__))\n"
    )
    (tmp_path / "mcp.toml").write_text('[[tool]]\nname = "m"\nfile = "t.py"\ndescription = "d"\n')
    manifest = app_mcp.load_app_manifest(tmp_path)
    spec = app_mcp.build_app_tool_spec(manifest, manifest.tools[0])
    got = asyncio.run(app_mcp._run_app_tool(spec, {}))
    assert os.path.realpath(got) == os.path.realpath(tmp_path)


def test_a_str_return_is_the_text_itself(served):
    assert served("plain_text") == "hello"
    assert served("json_text") == '{"a": 1}'


def test_a_structured_return_is_decoded(served):
    assert served("a_dict") == {"a": 1}


def _one_tool_spec(tmp_path):
    (tmp_path / "t.py").write_text("def main():\n    return 1\n")
    (tmp_path / "mcp.toml").write_text('[[tool]]\nname = "m"\nfile = "t.py"\ndescription = "d"\n')
    manifest = app_mcp.load_app_manifest(tmp_path)
    return app_mcp.build_app_tool_spec(manifest, manifest.tools[0])


def test_every_call_runs_on_the_engines_one_backend(tmp_path, monkeypatch):
    from fused_render import engine

    monkeypatch.setattr(app_mcp, "_app_interpreter", _fake_interpreter)
    spec = _one_tool_spec(tmp_path)
    seen = []
    backend = engine.get_backend()
    real = backend._execute_sync

    def spy(**kw):
        seen.append(backend)
        return real(**kw)

    monkeypatch.setattr(backend, "_execute_sync", spy)
    for _ in range(2):
        assert asyncio.run(app_mcp._run_app_tool(spec, {})) == 1
    assert seen == [backend, backend]


def test_the_interpreter_comes_from_the_engines_resolution(tmp_path, monkeypatch):
    from fused_render import engine

    spec = _one_tool_spec(tmp_path)
    asked = []

    async def resolved(path):
        asked.append(path)
        return "/the/interpreter", None

    monkeypatch.setattr(engine, "resolve_interpreter", resolved)
    assert asyncio.run(app_mcp._app_interpreter(spec)) == "/the/interpreter"
    assert asked == [str(tmp_path / "t.py")]


def test_a_missing_environment_is_a_clear_error(tmp_path, monkeypatch):
    from fused_render import engine

    spec = _one_tool_spec(tmp_path)

    async def unavailable(path):
        return None, {"ok": False, "error": {"message": "env not installed"}}

    monkeypatch.setattr(engine, "resolve_interpreter", unavailable)
    with pytest.raises(RuntimeError, match="env not installed"):
        asyncio.run(app_mcp._app_interpreter(spec))
