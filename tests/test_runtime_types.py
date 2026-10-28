"""`fused_render/static/fused.d.ts` — TypeScript declarations for `window.fused`.

The runtime (static/runtime.js) is the contract; the declarations restate it
for an editor. Two things can drift: a member added to the runtime and never
typed, or a member typed that the runtime does not export. Both are checked
here by reading the two files, in the string-contract style of
tests/test_runtime_upload.py — no TypeScript compiler in the Python suite.

The file is also served by the app at /static/fused.d.ts, which is how an
author outside this repository gets it.
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import fused_render
from fused_render.server import create_app

STATIC = Path(fused_render.__file__).parent / "static"
RUNTIME = (STATIC / "runtime.js").read_text(encoding="utf-8")
DTS = (STATIC / "fused.d.ts").read_text(encoding="utf-8")


def _js_object_members(literal_start: str) -> set[str]:
    """Identifiers at the start of each line of an object literal in runtime.js,
    from `<literal_start>` to its closing `};` — the keys of `window.fused = {`,
    `const ai = {`, and the rest. Comment lines are skipped."""
    rest = RUNTIME.split(literal_start, 1)[1]
    first_line = rest.split("\n", 1)[0]
    if "}" in first_line:
        # A one-line literal: `const terminal = { open: terminalOpen, run: terminalRun };`
        entries = first_line.split("}", 1)[0].split(",")
    else:
        entries = []
        for line in rest.split("\n  };", 1)[0].splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("//"):
                entries.append(stripped)
    members = set()
    for entry in entries:
        m = re.match(r"([A-Za-z_]\w*)\s*[:,]", entry.strip())
        if m:
            members.add(m.group(1))
    return members


def _dts_interface_members(name: str) -> set[str]:
    body = DTS.split(f"interface {name} {{", 1)[1].split("\n}", 1)[0]
    members = set()
    for line in body.splitlines():
        m = re.match(r"\s*(?:readonly\s+)?([A-Za-z_]\w*)\??\s*[:(<]", line)
        if m:
            members.add(m.group(1))
    return members


# The runtime object literal -> the interface that types it. Namespaces that
# are built as `const <name> = {` in runtime.js are checked member by member.
SURFACES = {
    "window.fused = {": "Fused",
    "const ai = {": "FusedAi",
    "const aiModels = {": "FusedAiModels",
    "const capture = {": "FusedCapture",
    "const terminal = {": "FusedTerminal",
    "const fileIndex = {": "FusedFileIndex",
    "const tasks = {": "FusedTasks",
    "const bots = {": "FusedBots",
    "const daemon = {": "FusedDaemon",
}


@pytest.mark.parametrize("literal,interface", list(SURFACES.items()))
def test_every_runtime_member_is_declared(literal, interface):
    runtime = _js_object_members(literal)
    assert runtime, f"could not read members of {literal!r} from runtime.js"
    declared = _dts_interface_members(interface)
    missing = runtime - declared
    assert not missing, f"{interface} lacks members the runtime exports: {sorted(missing)}"


@pytest.mark.parametrize("literal,interface", list(SURFACES.items()))
def test_no_declared_member_is_missing_from_the_runtime(literal, interface):
    runtime = _js_object_members(literal)
    declared = _dts_interface_members(interface)
    invented = declared - runtime
    assert not invented, f"{interface} declares members the runtime does not export: {sorted(invented)}"


def test_params_members_match():
    # `params` is an inline literal on window.fused, not a `const params = {`.
    surface = RUNTIME.split("window.fused = {", 1)[1].split("\n  };", 1)[0]
    inline = re.search(r"params:\s*\{([^}]*)\}", surface).group(1)
    runtime = {m.strip() for m in inline.split(",") if m.strip()}
    assert runtime == _dts_interface_members("FusedParams")


def test_the_file_is_a_global_script_declaration():
    # No import/export: a `/// <reference path>` or a tsconfig `include` is all
    # an author needs, and `fused` is a global in every rendered page.
    assert not re.search(r"^(import|export)\b", DTS, re.M)  # top-level only; `export(key)` is a tasks member
    assert "declare const fused: Fused;" in DTS
    assert "interface Window {" in DTS and "fused: Fused;" in DTS


def test_the_declarations_are_served_beside_the_runtime(tmp_path):
    client = TestClient(create_app(start_dir=str(tmp_path)))
    res = client.get("/static/fused.d.ts")
    assert res.status_code == 200
    assert "interface Fused {" in res.text
    assert res.text == DTS
