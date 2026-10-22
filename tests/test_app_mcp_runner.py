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


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mcp, "_app_interpreter", lambda app_dir: sys.executable)
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
    monkeypatch.setattr(app_mcp, "_app_interpreter", lambda app_dir: sys.executable)
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
