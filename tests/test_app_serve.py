"""`fused-render app serve <dir>`: the vendored stdio MCP server over an app's mcp.toml."""

import asyncio
import json
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="posix_spawn path")

MANIFEST = textwrap.dedent(
    """
    [[tool]]
    name = "add"
    description = "Adds."
    file = "t.py"
    entrypoint = "plus"

    [tool.pinned]
    b = 10
    """
)
SOURCE = "def plus(a: int, b: int = 0):\n    return {'sum': a + b}\n"


@pytest.fixture
def app_dir(tmp_path):
    (tmp_path / "mcp.toml").write_text(MANIFEST)
    (tmp_path / "t.py").write_text(SOURCE)
    return tmp_path


def test_the_backend_runs_a_named_entrypoint_without_a_result_variable():
    from fused_render.compute.backend import LocalPythonComputeBackend

    b = LocalPythonComputeBackend(timeout_seconds=60, python_executable=sys.executable)
    try:
        files = {"_params.json": json.dumps({"a": 2}).encode()}
        r = b._execute_sync("def plus(a):\n    return a + 1\n", input_files=files, entrypoint="plus")
    finally:
        if b._dispatcher is not None:
            b._dispatcher.stop()
    assert r.error is None, r
    assert json.loads(r.return_value) == 3


def test_a_missing_manifest_is_a_startup_error_on_stderr(tmp_path):
    p = subprocess.run(
        [sys.executable, "-m", "fused_render.cli", "app", "serve", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert p.returncode == 1
    assert p.stdout == ""
    assert "fused-render app serve:" in p.stderr


def test_a_host_lists_and_calls_the_tool_over_stdio(app_dir):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "fused_render.cli", "app", "serve", str(app_dir)],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                called = await session.call_tool("add", {"a": 5, "b": 99})
                return tools, called

    tools, called = asyncio.run(asyncio.wait_for(run(), 90))
    assert [t.name for t in tools] == ["add"]
    assert list(tools[0].inputSchema["properties"]) == ["a"]  # b is pinned, so hidden
    assert not called.isError, called
    assert json.loads(called.content[0].text) == {"sum": 15}  # the pin wins over the passed b
